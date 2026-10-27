#!/bin/bash
# Forced command for the `deploy` SSH key (see authorized_keys). CI calls:
#   docker save … | gzip | ssh deploy@host load
#   tar -c compose.yaml compose.prod.yaml | ssh deploy@host config
#   ssh deploy@host up <git-sha>
set -euo pipefail
cd /opt/glo-people
COMPOSE=(docker compose -f compose.yaml -f compose.prod.yaml)

read -r cmd arg extra <<<"${SSH_ORIGINAL_COMMAND:-}"
case "$cmd" in
  load)
    gunzip | docker load
    ;;
  config)
    # only the compose files; this script itself stays root-owned and is not updatable by CI
    tmp=$(mktemp -d)
    tar -x -C "$tmp" compose.yaml compose.prod.yaml
    install -m 644 "$tmp/compose.yaml" "$tmp/compose.prod.yaml" .
    rm -rf "$tmp"
    ;;
  up)
    [[ "$arg" =~ ^[0-9a-f]{7,40}$ && -z "${extra:-}" ]] || { echo "usage: up <git-sha>" >&2; exit 2; }
    docker image inspect "glo-people-api:$arg" "glo-people-web:$arg" >/dev/null
    sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$arg/" .env
    "${COMPOSE[@]}" up -d postgres mongo --wait
    "${COMPOSE[@]}" run --rm --no-deps api alembic upgrade head
    "${COMPOSE[@]}" up -d --wait --remove-orphans
    # keep the current and previous release for rollback
    for repo in glo-people-api glo-people-web; do
      docker images "$repo" --format '{{.CreatedAt}}\t{{.Tag}}' | sort -r | tail -n +3 | cut -f2 |
        xargs -r -I{} docker rmi "$repo:{}" >/dev/null || true
    done
    docker image prune -f >/dev/null
    echo "deployed $arg"
    ;;
  status)
    "${COMPOSE[@]}" ps
    ;;
  *)
    echo "allowed: load | config | up <sha> | status" >&2
    exit 2
    ;;
esac
