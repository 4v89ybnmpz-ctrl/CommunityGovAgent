"""Issue 治理使用的队列名称。公共模块只负责连接和确认，不绑定具体治理对象。"""

from govcommon.queueing import QueueTopology

ISSUE_ANALYSIS = QueueTopology(
    analysis="issue.analysis",
    dead_letter="issue.analysis.dlq",
    exchange="issue.analysis.dlx",
)
