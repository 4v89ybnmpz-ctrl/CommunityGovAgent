#!/usr/bin/env bash
# 初始化配置（若缺失）并启动全部容器。已有 runtime 配置不会被覆盖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先安装 Docker Engine 与 Compose 插件。" >&2
  exit 1
fi

if [[ ! -f issue-gov-agent/.env || ! -f issue-gov-agent/repository.yml ]]; then
  if [[ -x issue-gov-agent/.venv/bin/python ]]; then
    PYTHON=issue-gov-agent/.venv/bin/python
  else
    PYTHON=python3
  fi
  (cd issue-gov-agent && "$PYTHON" manage.py init)
  echo "已生成 issue-gov-agent/.env 和 repository.yml。"
  echo "请填写令牌、模型配置和接入仓库后重新执行 scripts/deploy.sh。"
  exit 1
fi

"$ROOT/scripts/compose" up -d --build "$@"
echo "等待接收服务就绪..."
PORT="$(grep -E '^WEBHOOK_PORT=' issue-gov-agent/.env | tail -n 1 | cut -d= -f2-)"
python3 - "${PORT:-7000}" <<'PY'
import sys, time, urllib.request
port = sys.argv[1]
deadline = time.time() + 90
while time.time() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=3) as response:
            if response.status == 200:
                raise SystemExit(0)
    except Exception:
        time.sleep(2)
print("接收服务未在 90 秒内就绪。查看日志：scripts/compose logs --tail=80 receiver", file=sys.stderr)
raise SystemExit(1)
PY
"$ROOT/scripts/compose" ps
echo "接收地址：http://127.0.0.1:${PORT:-7000}/webhook"
echo "扩容消费者：scripts/scale-consumers.sh 3"
