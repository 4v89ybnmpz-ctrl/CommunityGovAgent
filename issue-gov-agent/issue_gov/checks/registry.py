"""按检查类别找到工具。checks.tools 下导出 TOOL 的包会自动登记。"""

import importlib
import pkgutil
from typing import Any

from common.errors import ProcessingError
from issue_gov.checks.base import CheckTask

_TOOLS: dict[str, Any] = {}


def register(tool: Any) -> None:
    """登记一个检查工具。重复登记同一类别会覆盖旧工具。"""
    _TOOLS[tool.kind] = tool


def get_tool(kind: str) -> Any:
    """按类别取工具。未知类别不可重试。"""
    _ensure_loaded()
    try:
        return _TOOLS[kind]
    except KeyError:
        raise ProcessingError(f"未知检查任务：{kind}", retryable=False) from None


def _ensure_loaded() -> None:
    """加载 checks.tools 中尚未登记的工具。测试预先登记的类别不会被覆盖。"""
    import issue_gov.checks.tools as tools

    for module_info in pkgutil.iter_modules(tools.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{tools.__name__}.{module_info.name}")
        tool = getattr(module, "TOOL", None)
        if tool is not None and tool.kind not in _TOOLS:
            register(tool)


def run_tasks(context: Any, tasks: tuple[CheckTask, ...]) -> list[dict[str, Any]]:
    """按列表顺序执行。一个工具失败时不继续后面的检查。"""
    return [get_tool(task.kind).run(context, task.parameters) for task in tasks]
