#!/usr/bin/env bash
# verify.sh — canonical build gate (no make required).
set -euo pipefail
cd "$(dirname "$0")"
echo "== local-kb verify =="
npm test
echo "== all checks green =="
