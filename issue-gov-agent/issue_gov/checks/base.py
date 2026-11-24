"""检查任务只描述类别和参数。具体怎么检查由对应工具决定。"""

from dataclasses import dataclass, field
import json
from typing import Any, Mapping

# 规范性检查。参数为空表示使用当前的模板格式检查，不附加额外条件。
COMPLIANCE = "compliance"


@dataclass(frozen=True)
class CheckTask:
    """一条待执行的检查。

    kind 是工具注册名。parameters 只放该工具自己理解的参数，默认是空对象。
    """

    kind: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "parameters": dict(self.parameters)}


def default_checks() -> list[dict[str, Any]]:
    """入队时的默认检查列表：只做规范性检查，参数为空。"""
    return [{"kind": COMPLIANCE, "parameters": {}}]


def dump_checks(checks: list[dict[str, Any]] | None = None) -> str:
    """把检查列表存成稳定的 JSON，供事件表和消息体共用。"""
    return json.dumps(checks if checks is not None else default_checks(), ensure_ascii=False, separators=(",", ":"))


def parse_checks(raw: Any) -> tuple[CheckTask, ...]:
    """解析消息或数据库中的检查列表。缺省时使用默认规范性检查。

    Raises:
        ValueError: 列表为空，或某一项缺少类别、参数不是对象。
    """
    if raw is None or raw == "":
        raw = default_checks()
    elif isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("检查任务必须是 JSON 列表") from None
    if not isinstance(raw, list) or not raw:
        raise ValueError("检查任务必须是非空列表")
    tasks = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("检查任务必须是对象")
        kind = item.get("kind")
        parameters = item.get("parameters", {})
        if not isinstance(kind, str) or not kind.strip() or not isinstance(parameters, dict):
            raise ValueError("检查任务需要 kind 和 parameters")
        tasks.append(CheckTask(kind.strip(), dict(parameters)))
    return tuple(tasks)


def is_default_checks(raw: Any) -> bool:
    """缺省或与默认列表相同。无法解析时视为非默认，由调度器报错。"""
    try:
        return [task.to_dict() for task in parse_checks(raw)] == default_checks()
    except ValueError:
        return False
