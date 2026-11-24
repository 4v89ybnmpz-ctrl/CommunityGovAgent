"""PostgreSQL 路径的关键语句回归。SQLite 单测无法暴露方言差异（如 MAX 双参）。

无 PostgreSQL 时整组跳过：设置 PG_TEST_DATABASE_URL，
例如 postgresql://issue:issue@127.0.0.1:5432/issue_gov_test。
CI 中由 service 容器提供，本地验证可用 scripts/service compose up -d postgres。
"""

import os
import time
import unittest

import issue_gov  # 把仓库根目录放进导入路径，后续才能导入 common
from common.errors import IssueProcessingError

TEST_URL = os.environ.get("PG_TEST_DATABASE_URL", "").strip()

# SQLite 单测复用同一断言；PG 环境注入连接串后走真实 PostgreSQL。
if not TEST_URL:
    raise unittest.SkipTest("未设置 PG_TEST_DATABASE_URL，跳过 PostgreSQL 路径测试")


def _config(database_url: str):
    """构造最小服务配置，只用于数据库层语句验证。"""
    from issue_gov.settings import Config
    return Config(
        secret="test-secret", token="test-token",
        repositories=("example/repo",), database=":memory:",
        database_url=database_url, rabbitmq_url="",
    )


class PostgresPathTests(unittest.TestCase):
    """覆盖队列模式下消费者真正执行的数据库写入路径。"""

    @classmethod
    def setUpClass(cls):
        from common.pgstore import initialize
        from common.store import connect
        cls.url = TEST_URL
        initialize(cls.url)
        cls.config = _config(cls.url)
        cls.connect = staticmethod(lambda: connect(cls.config))

    def _reset(self, event_id="pg-evt-1", state="running", attempts=1):
        with self.connect() as db:
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM reports")
            db.execute("DELETE FROM api_cooldown")
            db.execute("""INSERT INTO events(event_id, repository, issue_number, updated_at,
                            attempts, next_attempt, state, checks)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (event_id, "example/repo", 1, time.time(), attempts, 0, state,
                        '[{"kind":"compliance","parameters":{}}]'))

    def test_save_failure_429_writes_cooldown_and_schedules_retry(self):
        """429 限流必须写入 api_cooldown 且任务回到 queued —— 回归：MAX(a,b) 在 PG 上非法。"""
        from issue_gov.worker import save_failure
        self._reset()
        config = _config(self.url)
        exc = IssueProcessingError("AtomGit 请求被限流，HTTP 429", True,
                                   status_code=429, retry_after=30)
        state, delay = save_failure(config, "pg-evt-1", 1, exc, time.time())
        self.assertEqual(state, "queued")
        self.assertGreaterEqual(delay, 30)
        with self.connect() as db:
            until = db.execute("SELECT until FROM api_cooldown WHERE name='atomgit'").fetchone()
            self.assertIsNotNone(until, "429 后必须写入 api_cooldown，否则限流冷却失效")

    def test_save_failure_429_keeps_longer_cooldown(self):
        """已有更长冷却时，新的 429 不得缩短它 —— 回归：MAX(until, excluded.until)。"""
        from issue_gov.worker import save_failure
        self._reset()
        config = _config(self.url)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (now + 300,))
        exc = IssueProcessingError("AtomGit 请求被限流，HTTP 429", True,
                                   status_code=429, retry_after=30)
        save_failure(config, "pg-evt-1", 1, exc, now)
        with self.connect() as db:
            until = db.execute("SELECT until FROM api_cooldown WHERE name='atomgit'").fetchone()[0]
        self.assertGreaterEqual(until, now + 299, "重复 429 不能把冷却时间改短")

    def test_save_failure_dead_letter_marks_failed(self):
        """全部模型密钥失败时任务标记 failed 且 dead_lettered=1。"""
        from issue_gov.worker import save_failure
        self._reset(attempts=5)
        config = _config(self.url)
        exc = IssueProcessingError("全部模型密钥均调用失败", retryable=False, keys_exhausted=True)
        state, _ = save_failure(config, "pg-evt-1", 5, exc, time.time())
        self.assertEqual(state, "failed")
        with self.connect() as db:
            row = db.execute("SELECT state, dead_lettered FROM events").fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["dead_lettered"], 1)

    def test_save_success_completes_and_reports(self):
        """成功路径写 completed 和 reports。"""
        from issue_gov.worker import save_success
        self._reset()
        config = _config(self.url)
        save_success(config, "pg-evt-1", 1,
                     {"status": "pass", "comment_action": "suppressed"}, time.time())
        with self.connect() as db:
            state = db.execute("SELECT state FROM events").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(state, "completed")
        self.assertEqual(count, 1)

    def test_claim_due_transitions_dispatching(self):
        """到期任务被原子领取为 dispatching，且不重复领取。"""
        from common.pgstore import claim_due
        self._reset(state="queued")
        now = time.time()
        first = claim_due(self.url, now)
        second = claim_due(self.url, now)
        self.assertEqual([row["event_id"] for row in first], ["pg-evt-1"])
        self.assertEqual(second, [], "同一任务不能被领取两次")
        with self.connect() as db:
            state = db.execute("SELECT state FROM events").fetchone()[0]
        self.assertEqual(state, "dispatching")

    def test_claim_due_recovers_stale_dispatching(self):
        """dispatching 超过 stale_after 秒的任务应被回收重新领取。"""
        from common.pgstore import claim_due
        now = time.time()
        self._reset(state="dispatching")
        with self.connect() as db:
            db.execute("UPDATE events SET updated_at = ?", (now - 120,))
        rows = claim_due(self.url, now, stale_after=60)
        self.assertEqual([row["event_id"] for row in rows], ["pg-evt-1"])

    def test_handle_delivery_uses_shared_locking_claim(self):
        """handle_delivery 竞争领取：同 event 并发调用只处理一次（借线程池模拟）。"""
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import issue_gov.worker as worker
        self._reset(state="dispatching", attempts=0)
        config = _config(self.url)
        calls = []

        def process(config_, repository, number, *, client, checks=None):
            time.sleep(0.02)
            calls.append(number)
            return {"status": "pass", "comment_action": "suppressed"}

        with patch.object(worker, "_process_issue", side_effect=process):
            with ThreadPoolExecutor(max_workers=4) as pool:
                actions = list(pool.map(
                    lambda _: worker.handle_delivery(
                        config, {"event_id": "pg-evt-1"}, False, client=object()),
                    range(4)))
        self.assertEqual(actions, ["ack"] * 4)
        self.assertEqual(calls, [1], "并发领取后只应执行一次处理")


if __name__ == "__main__":
    unittest.main()
