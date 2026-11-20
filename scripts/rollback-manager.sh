#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 1 ]] || { echo "Usage: bash $0 DEPLOY_DIR" >&2; exit 2; }
cd "$1/compose"
base=(docker compose --project-name local-mirrors --env-file .env -f docker-compose.yml)
"${base[@]}" -f docker-compose.manager.yml stop mirror-admin mirror-verifier
"${base[@]}" up -d --pull never --no-deps local-npm
