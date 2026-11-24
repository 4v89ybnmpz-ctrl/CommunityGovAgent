"""Issue 治理服务。配置在服务根目录，检查工具在 checks.tools，公共能力在 common。"""

import sys
from pathlib import Path

# 本机从 issue-gov-agent 启动时，同级目录 common 在仓库根上，需要能被直接导入。
_REPO = Path(__file__).resolve().parents[2]
if (_REPO / "common" / "__init__.py").is_file():
    _root = str(_REPO)
    if _root not in sys.path:
        sys.path.insert(0, _root)
