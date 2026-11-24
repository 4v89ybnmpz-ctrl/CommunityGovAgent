"""规范性检查的引用点。实现按能力放在 checks.tools.compliance。"""

import os
import signal
import subprocess

from issue_gov.checks.tools.compliance.check import check_issue
from issue_gov.checks.tools.compliance.comment import _format_template, format_compliance_check
from issue_gov.checks.tools.compliance.parse import (
    _fields, _required_hint, _sections, parse_template,
)
from issue_gov.checks.tools.compliance.recommend import LLM_TIMEOUT, RECOMMEND_PROMPT, recommend_template

__all__ = [
    "LLM_TIMEOUT", "RECOMMEND_PROMPT", "_fields", "_format_template", "_required_hint",
    "_sections", "check_issue", "format_compliance_check", "os", "parse_template",
    "recommend_template", "signal", "subprocess",
]
