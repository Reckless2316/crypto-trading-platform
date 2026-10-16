#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/deploy/docker-compose.yml" --project-directory "$ROOT")
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
