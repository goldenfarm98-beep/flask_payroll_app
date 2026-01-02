#!/usr/bin/env bash
set -euo pipefail

reset_compose() {
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose down -v
    docker-compose up -d db
    return
  fi
  docker compose down -v
  docker compose up -d db
}

reset_compose

if [ -d "backups" ]; then
  rm -f backups/backup_*.json
fi
