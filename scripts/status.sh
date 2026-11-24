#!/usr/bin/env bash
# 查看容器状态、存活和就绪检查。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/compose" ps
echo
PORT="$(grep -E '^WEBHOOK_PORT=' issue-gov-agent/.env | tail -n 1 | cut -d= -f2-)"
python3 - "${PORT:-7000}" <<'PY'
import sys, urllib.request
port = sys.argv[1]
for path in ("/health", "/ready"):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        print(f"{path}：{response.read().decode()}")
PY
