"""兼容引用。规范性检查工具在 issue_gov.checks.tools.compliance。"""

from issue_gov.checks.tools.compliance.run import (  # noqa: F401
    ComplianceTool, _recommend, analyze_issue, should_publish_comment,
)
