#!/usr/bin/env bash
# Holt den neuesten Stand aus GitHub und startet den Bot neu. Zustand (state/) und .env bleiben erhalten.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
docker compose ps
