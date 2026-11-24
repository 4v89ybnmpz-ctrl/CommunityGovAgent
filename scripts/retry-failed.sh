#!/usr/bin/env bash
# 将数据库中的失败任务重新入队，并由正在运行的消费者继续处理。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/compose" exec -T consumer python worker.py --retry-failed
