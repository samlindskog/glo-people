DEV  = docker compose -f compose.yaml -f compose.dev.yaml
PROD = docker compose -f compose.yaml -f compose.prod.yaml
API  = $(DEV) exec api

.PHONY: dev down logs ps test test-api test-web e2e sandbox-up sandbox-down e2e-sandbox lint create-user set-password import embed properties classify migrate psql mongosh prod-create-user prod-import prod-embed prod-boards-link prod-migrate ops-health ops-tunnel-key prod-backup prod-push-data prod-env prod-status prod-aop-on prod-aop-off

dev:            ## start the dev stack on http://localhost:8080 (live reload)
	$(DEV) up -d --build
	@echo "→ http://localhost:8080"

down:
	$(DEV) down

logs:
	$(DEV) logs -f --tail=100

ps:
	$(DEV) ps

test: test-api test-web

test-api:       ## pytest inside the api container (uses glo_test databases)
	$(API) env TEST_DATABASE_URL=postgresql+asyncpg://glo:glo@postgres:5432/glo_test \
	  TEST_MONGO_URL=mongodb://mongo:27017 pytest -q

test-web:
	cd web && npm test

e2e:            ## Playwright against the running dev stack (real agents, real Firecrawl: spends a little)
	cd e2e && npx playwright test

# The sandbox: a separate compose project with its own data, fake agents and no provider keys,
# on http://localhost:8081. For e2e that must not touch the dev data (which is production data).
SANDBOX = docker compose -f compose.yaml -f compose.dev.yaml -f compose.e2e.yaml

sandbox-up:     ## start the sandbox stack (fakes, own database) on http://localhost:8081
	env -u OPENAI_API_KEY -u FIRECRAWL_API_KEY $(SANDBOX) up -d --build
	@echo "→ http://localhost:8081 (sandbox: fake agents, disposable data)"

sandbox-down:   ## stop the sandbox and drop its data
	$(SANDBOX) down -v

e2e-sandbox: sandbox-up   ## Playwright against the sandbox: every spec runs, nothing real is touched
	@for i in $$(seq 1 60); do curl -sf -m 3 localhost:8081/api/health >/dev/null 2>&1 && curl -sf -m 3 localhost:8081/ >/dev/null 2>&1 && break; sleep 2; done
	cd e2e && BASE_URL=http://localhost:8081 COMPOSE="$(SANDBOX)" npx playwright test

lint:
	cd api && uv run ruff check app tests alembic
	cd web && npx tsc -b && npx oxlint src

# make create-user USER=sam ROLE=writer   (prompts for the password)
create-user:
	$(API) python -m app.cli create-user $(USER) --role $(or $(ROLE),reader)

set-password:
	$(API) python -m app.cli set-password $(USER)

# make import FILE=data/members.csv TIER=member   (FILE must be under ./data)
import:
	$(API) python -m app.cli import /data/$(notdir $(FILE)) --tier $(TIER)

embed:          ## sync property-name vectors and backfill passage vectors (FIELD=bio to limit)
	$(API) python -m app.cli embed $(if $(FIELD),--field $(FIELD),)

properties:     ## the property catalog with coverage
	$(API) python -m app.cli properties

classify:       ## suggest property_specs entries for untyped properties from the data
	$(API) python -m app.cli classify

migrate:        ## apply alembic migrations, wrap legacy values into histories, normalise (dev)
	$(API) sh -c 'alembic upgrade head && python -m app.cli migrate-history && python -m app.cli normalize --all'

psql:
	$(DEV) exec postgres psql -U glo glo

mongosh:
	$(DEV) exec mongo mongosh glo

# --- production (glousfr1, via your `sam` SSH alias) -----------------------------------------
# sudo on glousfr1 requires sam's password (as on uscentral1), so every helper here allocates a
# tty with `ssh -t` for sudo to prompt on.
PROD_SSH ?= glousfr1
PROD_EXEC = ssh -t $(PROD_SSH) sudo docker exec -it glo-people-api-1

# make prod-create-user USER=sam ROLE=writer   (prompts for the password on the server)
prod-create-user:
	$(PROD_EXEC) python -m app.cli create-user $(USER) --role $(or $(ROLE),reader)

# make prod-import FILE=data/members.csv TIER=member
prod-import:
	scp -q "$(FILE)" $(PROD_SSH):/tmp/glo-import.csv
	ssh -t $(PROD_SSH) 'sudo docker cp /tmp/glo-import.csv glo-people-api-1:/tmp/$(notdir $(FILE)) && rm /tmp/glo-import.csv'
	ssh -t $(PROD_SSH) sudo docker exec glo-people-api-1 python -m app.cli import /tmp/$(notdir $(FILE)) --tier $(TIER)
	ssh -t $(PROD_SSH) sudo docker exec -u 0 glo-people-api-1 rm /tmp/$(notdir $(FILE))

prod-embed:
	ssh -t $(PROD_SSH) sudo docker exec glo-people-api-1 python -m app.cli embed

prod-boards-link:   ## refresh the people the boards refer to; link talent entries by name (--create: make the rest)
	ssh -t $(PROD_SSH) sudo docker exec -it glo-people-api-1 python -m app.cli boards-link $(ARGS)

# The ops tunnel (deploy/ops-tunnel.sh): the laptop's key pair for it, and the health of prod
# over it. The MCP itself is registered in Claude Code: claude mcp add --transport http glo-ops http://10.89.0.1:8800/ops/mcp
ops-tunnel-key:     ## make the laptop's WireGuard key pair for the ops tunnel (prints the public key)
	@umask 077; test -f "$$HOME/.config/glo-people/ops-tunnel.key" || { mkdir -p "$$HOME/.config/glo-people"; wg genkey > "$$HOME/.config/glo-people/ops-tunnel.key"; }
	@echo "laptop public key: $$(wg pubkey < "$$HOME/.config/glo-people/ops-tunnel.key")"
	@echo "laptop private key is in ~/.config/glo-people/ops-tunnel.key (paste into WireGuard.app)"

ops-health:         ## prod's build, uptime, schema and databases, over the ops tunnel
	curl -s -m 5 http://10.89.0.1:8800/ops/health | python3 -m json.tool

# The dev stack holds the primary copy of the people data (the live imports land there first).
# prod-push-data replaces prod's `people` and `fields` collections with dev's — after taking a
# backup of prod into ./backups — and then runs prod-migrate so vectors, lookup keys and shown
# values follow. Runs, users, keys and the activity log stay prod's own (Postgres is untouched).
BACKUPS ?= backups

prod-backup:    ## dump prod's Mongo database (people + properties) into ./backups
	mkdir -p $(BACKUPS)
	ssh -t $(PROD_SSH) 'sudo docker exec glo-people-mongo-1 mongodump --quiet -d glo --gzip --archive=/tmp/glo-prod.archive.gz && sudo docker cp glo-people-mongo-1:/tmp/glo-prod.archive.gz /tmp/glo-prod.archive.gz && sudo chown $$(id -un) /tmp/glo-prod.archive.gz'
	scp -q $(PROD_SSH):/tmp/glo-prod.archive.gz "$(BACKUPS)/prod-glo-$$(date -u +%Y%m%dT%H%M%SZ).archive.gz"
	ssh $(PROD_SSH) rm -f /tmp/glo-prod.archive.gz
	@ls -la $(BACKUPS) | tail -1

prod-push-data: prod-backup   ## replace prod's people and properties with dev's, then prod-migrate
	$(DEV) exec -T mongo mongodump --quiet -d glo --gzip --archive=/tmp/glo-dev.archive.gz
	docker cp glo-people-mongo-1:/tmp/glo-dev.archive.gz /tmp/glo-dev.archive.gz
	scp -q /tmp/glo-dev.archive.gz $(PROD_SSH):/tmp/glo-dev.archive.gz && rm /tmp/glo-dev.archive.gz
	ssh -t $(PROD_SSH) 'sudo docker cp /tmp/glo-dev.archive.gz glo-people-mongo-1:/tmp/glo-dev.archive.gz && sudo docker exec glo-people-mongo-1 mongorestore --quiet --drop --gzip --archive=/tmp/glo-dev.archive.gz && sudo docker exec glo-people-mongo-1 rm /tmp/glo-dev.archive.gz && rm /tmp/glo-dev.archive.gz'
	$(MAKE) prod-migrate

# make prod-env            copies these keys from the local .env to /opt/glo-people/.env (add or
#                          replace; values are never printed) and restarts the api to pick them up
PROD_ENV_KEYS ?= OPENAI_API_KEY FIRECRAWL_API_KEY LOGFIRE_TOKEN LOGFIRE_PROJECT_URL AGENT_MODEL EMBEDDING_PROVIDER
prod-env:
	@umask 077; for k in $(PROD_ENV_KEYS); do grep -E "^$$k=.+" .env || echo "warning: $$k is not set locally" >&2; done | grep -E "^[A-Z_]+=" > /tmp/glo-env-sync
	scp -pq /tmp/glo-env-sync deploy/env-merge.sh $(PROD_SSH):/tmp/ && rm /tmp/glo-env-sync
	ssh -t $(PROD_SSH) 'sudo sh /tmp/env-merge.sh /tmp/glo-env-sync /opt/glo-people/.env && rm /tmp/glo-env-sync /tmp/env-merge.sh && cd /opt/glo-people && sudo docker compose -f compose.yaml -f compose.prod.yaml up -d api'

# Prod never runs alembic on its own: run this after deploying a release that adds a migration.
# Applies the schema, wraps legacy values into histories, syncs property-name and passage vectors,
# and (re)parses the normalised values of typed properties.
prod-migrate:
	ssh -t $(PROD_SSH) "sudo docker exec glo-people-api-1 sh -c 'alembic upgrade head && python -m app.cli migrate-history && python -m app.cli fold-property title_enriched role --yes && python -m app.cli fold-property emails email --yes && python -m app.cli fold-property phones phone_number --yes && python -m app.cli repair-text --yes && python -m app.cli embed && python -m app.cli normalize --all && python -m app.cli recompute-shown --yes --sample 20'"

prod-status:
	ssh -t $(PROD_SSH) 'sudo docker ps --format "{{.Names}}\t{{.Status}}"; free -h | sed -n 2,3p'

# Authenticated Origin Pulls: enforce only after AOP is enabled for the zone in Cloudflare.
prod-aop-on:
	ssh -t $(PROD_SSH) 'echo "ssl_verify_client on;" | sudo tee /opt/glo-people/nginx/aop.conf >/dev/null && sudo docker exec glo-people-nginx-1 nginx -s reload'

prod-aop-off:
	ssh -t $(PROD_SSH) 'echo "ssl_verify_client optional;" | sudo tee /opt/glo-people/nginx/aop.conf >/dev/null && sudo docker exec glo-people-nginx-1 nginx -s reload'
