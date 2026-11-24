"""规范性检查工具：按解析、核对、推荐、评论四个模块完成一次检查。"""

import json
import logging
import time
from typing import Any

import yaml

from common.errors import ProcessingError
from issue_gov.checks.base import COMPLIANCE, is_default_checks
import issue_gov.checks.tools.compliance.check as compliance_check
import issue_gov.checks.tools.compliance.comment as compliance_comment
import issue_gov.checks.tools.compliance.parse as compliance_parse
import issue_gov.checks.tools.compliance.recommend as compliance_recommend

LOG = logging.getLogger("issue-gov-webhook")


def should_publish_comment(summary: dict, *, previously_commented: bool = False) -> bool:
    """根据分析摘要决定是否允许新增或更新评论。

    Args:
        summary: 分析摘要，包含 status 和 recommendation_status。
        previously_commented: 此前是否已有检查评论或待确认的评论发送记录。
    Returns:
        模板配置故障始终为 False；通过检查时仅在此前有评论时为 True；未通过时为 True。
    """
    if summary.get("status") == "template_error":
        return False
    # 通过检查时，若此前已有评论，则通知“检查通过”，否则不新增评论
    if summary.get("status") == "pass" or (
            summary.get("status") == "no_match" and summary.get("recommendation_status") == "not_applicable"):
        return previously_commented
    # 检查未通过，需要发评论
    return True


def _recommend(
    title: str, body: str, templates: list[dict[str, Any]],
    agent_api_key: str, model: str, base_url: str,
    agent_api_keys: tuple[str, ...] | None, retries: int, interval: float,
) -> dict[str, Any]:
    """调用模板推荐。未提供密钥列表时保持单次调用；提供时按密钥轮换。

    鉴权失败立即换下一个密钥。超时和进程启动失败按间隔重试当前密钥，用尽后再切换。
    结果格式不对只重试调用，不计入密钥耗尽，也不因此进入死信。
    配置缺失立即失败，不占用重试间隔。密钥本身全部失败后标记 keys_exhausted。
    """
    if not agent_api_keys:
        return compliance_recommend.recommend_template(title, body, templates, agent_api_key, model, base_url)
    last: ProcessingError | None = None
    invalid_attempts = 0
    for index, key in enumerate(agent_api_keys, start=1):
        attempt = 0
        while True:
            try:
                return compliance_recommend.recommend_template(title, body, templates, key, model, base_url)
            except ProcessingError as exc:
                if "未配置" in str(exc):
                    raise
                last = exc
                if exc.failure == "invalid_output":
                    invalid_attempts += 1
                    if invalid_attempts > retries:
                        raise ProcessingError(
                            "模型未返回有效的推荐结果，未能完成模板推荐", retryable=True) from None
                    time.sleep(interval)
                    continue
                if exc.failure == "auth":
                    break
                if attempt < retries:
                    time.sleep(interval)
                    attempt += 1
                    continue
                break
        if index < len(agent_api_keys):
            LOG.warning("模型密钥连续失败，切换下一个密钥：序号=%s/%s，原因=%s", index, len(agent_api_keys), last)
    raise ProcessingError("全部模型密钥均调用失败", retryable=False, keys_exhausted=True) from None


def analyze_issue(
    title: str, body: str, template_files: list[dict[str, str]],
    agent_api_key: str, model: str = "", base_url: str = "", *,
    repository: str, number: int, maintainers: tuple[str, ...] = (),
    previously_commented: bool = False,
    agent_api_keys: tuple[str, ...] | None = None,
    llm_key_retries: int = 2,
    llm_key_retry_interval: float = 10,
    checks: Any = None,
) -> dict[str, Any]:
    """执行规范性检查，生成结果摘要与可直接发布的完整评论。

    checks 仅在调度器把默认规范性检查交给本工具时出现。其他类别由 run_checks 分发。
    """
    if not is_default_checks(checks):
        raise ProcessingError("规范性检查不负责其他检查类别", retryable=False)
    templates = []
    template_problems = []
    if not template_files:
        template_problems.append({"source": repository,
                                  "message": "未找到可用的 YAML 或 Markdown Issue 模板，请添加模板或检查组织公共模板配置。"})
    for file in template_files:
        try:
            templates.append(compliance_parse.parse_template(file["content"], file["filename"]))
        except yaml.YAMLError:
            template_problems.append({"source": file["source"],
                                      "message": "YAML 语法无效，请检查缩进、引号、括号及标签。"})
        except ValueError as exc:
            template_problems.append({"source": file["source"], "message": str(exc)})
    if template_problems:
        result = {"status": "template_error", "template_problems": template_problems}
    else:
        result = compliance_check.check_issue(title, body, templates)
    if result["status"] == "no_match":
        result["recommendation"] = _recommend(
            title, body, templates, agent_api_key, model, base_url,
            agent_api_keys, llm_key_retries, llm_key_retry_interval)
    recommendation = result.get("recommendation", {})
    maintainer_suggestion = None
    if result["status"] == "template_error":
        maintainer_suggestion = {
            "reason": "\n".join(f"{p['source']}：{p['message']}" for p in template_problems),
            "suggestion": "请修复上述仓库模板配置问题。",
            "mtr": json.dumps(maintainers, ensure_ascii=False),
        }
    elif result["status"] == "no_match" and recommendation.get("status") == "not_applicable":
        maintainer_suggestion = {
            "reason": recommendation["reason"],
            "suggestion": recommendation["maintainer"],
            "mtr": json.dumps(maintainers, ensure_ascii=False),
        }

    summary = {"status": result["status"], "recommendation_status": recommendation.get("status")}
    publish_comment = should_publish_comment(summary, previously_commented=previously_commented)
    comment = ""
    if publish_comment:
        comment = compliance_comment.format_compliance_check(result, templates=templates)

    return {
        "summary": summary,
        "comment": comment,
        "publish_comment": publish_comment,
        "maintainer_suggestion": maintainer_suggestion,
    }


class ComplianceTool:
    """规范性检查工具。参数必须为空；具体步骤在同目录的解析、核对、推荐和评论模块中。"""

    kind = COMPLIANCE

    def run(self, context, parameters: dict) -> dict:
        if parameters:
            raise ProcessingError("规范性检查暂不接受参数", retryable=False)
        return analyze_issue(
            context.title, context.body, context.template_files,
            context.agent_api_key, context.model, context.base_url,
            repository=context.repository, number=context.number,
            maintainers=context.maintainers, previously_commented=context.previously_commented,
            agent_api_keys=context.agent_api_keys, llm_key_retries=context.llm_key_retries,
            llm_key_retry_interval=context.llm_key_retry_interval,
        )
