DEV  = docker compose -f compose.yaml -f compose.dev.yaml
PROD = docker compose -f compose.yaml -f compose.prod.yaml
API  = $(DEV) exec api

.PHONY: dev down logs ps test test-api test-web e2e lint create-user set-password import embed fields psql mongosh prod-create-user prod-import prod-embed prod-status prod-aop-on prod-aop-off

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

e2e:            ## Playwright against the running dev stack
	cd e2e && npx playwright test

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

embed:          ## backfill embeddings for every embedded field (FIELD=bio to limit)
	$(API) python -m app.cli embed $(if $(FIELD),--field $(FIELD),)

fields:
	$(API) python -m app.cli fields

psql:
	$(DEV) exec postgres psql -U glo glo

mongosh:
	$(DEV) exec mongo mongosh glo

# --- production (glousfr1, via your `sam` SSH alias) -----------------------------------------
PROD_SSH ?= glousfr1
PROD_EXEC = ssh -t $(PROD_SSH) sudo docker exec -it glo-people-api-1

# make prod-create-user USER=sam ROLE=writer   (prompts for the password on the server)
prod-create-user:
	$(PROD_EXEC) python -m app.cli create-user $(USER) --role $(or $(ROLE),reader)

# make prod-import FILE=data/members.csv TIER=member
prod-import:
	scp -q "$(FILE)" $(PROD_SSH):/tmp/glo-import.csv
	ssh $(PROD_SSH) 'sudo docker cp /tmp/glo-import.csv glo-people-api-1:/tmp/$(notdir $(FILE)) && rm /tmp/glo-import.csv'
	ssh $(PROD_SSH) sudo docker exec glo-people-api-1 python -m app.cli import /tmp/$(notdir $(FILE)) --tier $(TIER)
	ssh $(PROD_SSH) sudo docker exec glo-people-api-1 rm /tmp/$(notdir $(FILE))

prod-embed:
	ssh $(PROD_SSH) sudo docker exec glo-people-api-1 python -m app.cli embed

prod-status:
	ssh $(PROD_SSH) 'sudo docker ps --format "{{.Names}}\t{{.Status}}"; free -h | sed -n 2,3p'

# Authenticated Origin Pulls: enforce only after AOP is enabled for the zone in Cloudflare.
prod-aop-on:
	ssh $(PROD_SSH) 'echo "ssl_verify_client on;" | sudo tee /opt/glo-people/nginx/aop.conf >/dev/null && sudo docker exec glo-people-nginx-1 nginx -s reload'

prod-aop-off:
	ssh $(PROD_SSH) 'echo "ssl_verify_client optional;" | sudo tee /opt/glo-people/nginx/aop.conf >/dev/null && sudo docker exec glo-people-nginx-1 nginx -s reload'
