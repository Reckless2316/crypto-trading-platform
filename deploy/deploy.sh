#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose -f "$ROOT/deploy/docker-compose.yml" --project-directory "$ROOT")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose -f "$ROOT/deploy/docker-compose.yml" --project-directory "$ROOT")
else
  echo "Docker Compose is unavailable (install the Docker Compose plugin or docker-compose)." >&2
  exit 1
fi
COMMAND="${1:-help}"

case "$COMMAND" in
  start|up)
    "${COMPOSE[@]}" up -d --build
    echo "SURGE paper dashboard: http://127.0.0.1:8000"
    echo "Check deployment: $0 status"
    ;;
  stop|down)
    "${COMPOSE[@]}" down
    ;;
  restart)
    "${COMPOSE[@]}" up -d --build --force-recreate
    ;;
  status)
    "${COMPOSE[@]}" ps
    ;;
  logs)
    "${COMPOSE[@]}" logs --tail=100 -f
    ;;
  config)
    "${COMPOSE[@]}" config
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs|config}"
    exit 2
    ;;
esac
