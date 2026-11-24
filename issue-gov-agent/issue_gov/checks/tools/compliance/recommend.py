"""规范性检查：在格式未匹配时调用模型推荐模板。"""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Any

from govcommon.errors import ProcessingError
from issue_gov.checks.tools.compliance.parse import _fields
from issue_gov.paths import config_dir

LLM_TIMEOUT = 120
RECOMMEND_PROMPT = config_dir() / "recommend_template.md"

def recommend_template(
    title: str, body: str, templates: list[dict[str, Any]],
    agent_api_key: str, model: str = "", base_url: str = "",
) -> dict[str, Any]:
    """启动一次 Claude Code 任务，调用配置的模型推荐模板，不修改格式检查结果。

    Args:
        title: Issue 原始标题。
        body: Issue 原始 Markdown 正文。
        templates: 仓库候选模板字典列表。
        agent_api_key: Claude Code 兼容接口的 Bearer 认证密钥；为空时不调用模型。
        model: 服务商提供的模型名称。
        base_url: 服务商提供的 Anthropic 兼容接口地址。
    Returns:
        一个字典，包含四个字段：
            status 为 recommended / not_applicable / insufficient_info；
            template 为推荐的模板名称；
            reason 为原因；
            maintainer 为可选的维护者建议文本。
    Raises:
        ProcessingError: 配置缺失、调用失败、超时或响应无效；交由任务队列重试。
    """
    if not agent_api_key.strip():
        raise ProcessingError("未配置 Agent Key，无法执行模板推荐")
    if not model.strip() or not base_url.strip():
        raise ProcessingError("未配置 LLM_BASE_URL 或 LLM_MODEL，无法执行模板推荐")
    # 将模板信息整理为json传给模型
    candidates = [{
        "name": item["name"], "description": item.get("description", ""),
        "title": item.get("title", ""),
        "markdown_body": item.get("markdown_body", ""),
        "fields": [{"label": field["attributes"]["label"],
                    "description": field["attributes"].get("description", ""),
                    "type": field["type"], "options": field["attributes"].get("options", []),
                    "placeholder": field["attributes"].get("placeholder", ""),
                    "value": field["attributes"].get("value", ""),
                    "required": field.get("validations", {}).get("required", False)}
                   for field in _fields(item)],
    } for item in templates]

    # 启动子进程运行 Claude Code
    try:
        # 准备 Claude Code 的环境变量
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
               if key in os.environ}
        env.update({
            "ANTHROPIC_AUTH_TOKEN": agent_api_key,
            "ANTHROPIC_BASE_URL": base_url.strip(),
            "API_TIMEOUT_MS": str(LLM_TIMEOUT * 1000),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        })

        # 准备 Claude Code 命令和数据
        command = [
            "claude", "--bare", "-p", "--model", model.strip(),
            "--tools", "", "--no-session-persistence", "--max-turns", "3",
            "--output-format", "json",
            "--system-prompt", RECOMMEND_PROMPT.read_text(encoding="utf-8"),
        ]
        data = json.dumps({"issue": {"title": title, "body": body},
                           "templates": candidates}, ensure_ascii=False)

        # 新建子进程和临时目录，运行 Claude Code
        with tempfile.TemporaryDirectory(prefix="issue-gov-") as directory:
            env["CLAUDE_CONFIG_DIR"] = str(Path(directory) / ".claude")
            with subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=directory, env=env, start_new_session=True,
            ) as process:
                try:
                    output, stderr = process.communicate(data, timeout=LLM_TIMEOUT)
                finally:
                    # 清理子进程
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

        # 解析输出
        try:
            envelope = json.loads(output)
        except ValueError:
            raise ValueError(stderr.strip() or output.strip() or "Claude Code 未返回结果") from None
        if not isinstance(envelope, dict):
            raise ValueError("Claude Code 返回格式无效")
        if (process.returncode != 0 or envelope.get("type") != "result"
                or envelope.get("subtype") != "success" or envelope.get("is_error") is not False):
            raise ValueError(envelope.get("result") or envelope.get("errors")
                             or stderr.strip() or "Claude Code 调用失败")
        # 解析 Agent 推荐结果
        suggestion = json.loads(envelope["result"])
        if not isinstance(suggestion, dict):
            raise ValueError("推荐结果格式无效")
        status = suggestion["status"]
        name = suggestion["template"]
        reason = suggestion["reason"]
        maintenance = suggestion.get("maintainer")
        # 校验推荐状态
        if status not in {"recommended", "not_applicable", "insufficient_info"}:
            raise ValueError("推荐状态无效")
        # 校验模板名
        if status == "recommended":
            if name not in {t["name"] for t in templates}:
                raise ValueError("推荐模板无效")
        elif name is not None:
            raise ValueError("未推荐时不能指定模板")
        # 校验推荐理由
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("推荐理由无效")
        # 校验维护者建议
        if status == "not_applicable" and maintenance is None:
            raise ValueError("没有适用模板时须提供维护者建议")
        if maintenance is not None:
            if not isinstance(maintenance, str) or not 1 <= len(maintenance.strip()) <= 500:
                raise ValueError("维护者建议无效")
            maintenance = maintenance.strip()
        return {"status": status, "template": name, "reason": reason.strip(),
                "maintainer": maintenance}
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "模型响应超时，未能完成模板推荐。"
        elif isinstance(exc, OSError):
            reason = "推荐程序运行失败，未能完成模板推荐。"
        else:
            reason = "模型未返回有效的推荐结果，未能完成模板推荐。"
        raise ProcessingError(reason) from None

