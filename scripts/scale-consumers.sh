#!/usr/bin/env bash
# 调整消费者副本数。示例：scripts/scale-consumers.sh 3
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[1-9][0-9]*$ ]]; then
  echo "用法：scripts/scale-consumers.sh 副本数" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/compose" up -d --scale "consumer=$1" --no-deps
"$ROOT/scripts/compose" ps consumer
