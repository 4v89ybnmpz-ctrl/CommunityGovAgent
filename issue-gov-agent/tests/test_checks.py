"""检查任务列表、默认规范性检查和按类别调度。"""

from pathlib import Path
import sys
import unittest


from govcommon.errors import IssueProcessingError
from issue_gov.analysis import CheckContext, run_checks
from issue_gov.checks.base import default_checks, is_default_checks, parse_checks
from issue_gov.checks.tools.compliance.run import ComplianceTool
from issue_gov.checks.registry import register, run_tasks


def _context() -> CheckContext:
    return CheckContext("标题", "正文", [], "key", "model", "https://models.example",
                        "example/project", 1, (), False, None, 2, 10)


class CheckModelTests(unittest.TestCase):
    def test_default_is_compliance_with_empty_parameters(self):
        self.assertEqual(default_checks(), [{"kind": "compliance", "parameters": {}}])
        self.assertTrue(is_default_checks(None))
        self.assertTrue(is_default_checks(default_checks()))
        self.assertEqual(parse_checks(None)[0].kind, "compliance")
        self.assertEqual(parse_checks(None)[0].parameters, {})

    def test_compliance_tool_rejects_parameters(self):
        with self.assertRaisesRegex(IssueProcessingError, "暂不接受参数"):
            ComplianceTool().run(_context(), {"focus": "标题"})

    def test_unknown_kind_is_not_retried(self):
        with self.assertRaises(IssueProcessingError) as raised:
            run_checks("标题", "正文", [], "key", repository="example/project", number=1,
                       checks=[{"kind": "not-a-check", "parameters": {}}])
        self.assertFalse(raised.exception.retryable)
        self.assertIn("not-a-check", str(raised.exception))

    def test_each_task_uses_its_own_tool(self):
        seen = []

        class Quality:
            kind = "quality"

            def run(self, context, parameters):
                seen.append((context.number, dict(parameters)))
                return {"summary": {"status": "pass", "recommendation_status": None},
                        "comment": "质量检查", "publish_comment": False, "maintainer_suggestion": None}

        register(Quality())
        result = run_tasks(_context(), parse_checks([
            {"kind": "quality", "parameters": {"min_length": 20}},
            {"kind": "quality", "parameters": {}},
        ]))
        self.assertEqual(seen, [(1, {"min_length": 20}), (1, {})])
        self.assertEqual(result[0]["comment"], "质量检查")


if __name__ == "__main__":
    unittest.main()
