#!/usr/bin/env python3
"""验证接收服务、消息队列和多个消费者在并发下只处理一次，并检查死信。

默认读取 issue-gov-agent/.env 里的连接串，把主机改到本机映射端口，并使用独立验证库。
不调用 AtomGit 或模型。先启动基础设施：scripts/service compose up -d postgres rabbitmq
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def _runtime_values() -> dict[str, str]:
    """读取服务根目录的 .env。文件不存在时返回空映射。"""
    path = ROOT / "issue-gov-agent" / ".env"
    if not path.is_file():
        return {}
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _expose(url: str, port: str) -> str:
    """把容器主机名换成 127.0.0.1，并改用宿主机映射端口。"""
    parts = urllib.parse.urlsplit(url)
    auth = ""
    if parts.username is not None:
        auth = urllib.parse.quote(parts.username, safe="")
        if parts.password is not None:
            auth += ":" + urllib.parse.quote(parts.password, safe="")
        auth += "@"
    return urllib.parse.urlunsplit((parts.scheme, f"{auth}127.0.0.1:{port}", parts.path, parts.query, parts.fragment))


def _apply_runtime_urls() -> None:
    """未显式指定连接串时，用 issue-gov-agent/.env 中的账号连接本机端口。"""
    values = _runtime_values()
    if not os.environ.get("DATABASE_URL"):
        stored = values.get("DATABASE_URL", "")
        port = values.get("POSTGRES_PORT") or "54329"
        if stored:
            prefix, _, _name = _expose(stored, port).rpartition("/")
            os.environ["DATABASE_URL"] = f"{prefix}/issue_gov_verify"
        else:
            user = urllib.parse.quote(values.get("POSTGRES_USER") or "issue", safe="")
            password = urllib.parse.quote(values.get("POSTGRES_PASSWORD") or "issue", safe="")
            os.environ["DATABASE_URL"] = f"postgresql://{user}:{password}@127.0.0.1:{port}/issue_gov_verify"
    if not os.environ.get("RABBITMQ_URL"):
        stored = values.get("RABBITMQ_URL", "")
        port = values.get("RABBITMQ_PORT") or "56729"
        if stored:
            os.environ["RABBITMQ_URL"] = _expose(stored, port)
        else:
            user = urllib.parse.quote(values.get("RABBITMQ_USER") or "issue", safe="")
            password = urllib.parse.quote(values.get("RABBITMQ_PASSWORD") or "issue", safe="")
            os.environ["RABBITMQ_URL"] = f"amqp://{user}:{password}@127.0.0.1:{port}/%2F"


os.environ["SERVICE_MODE"] = "queue"
_apply_runtime_urls()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "issue-gov-agent"))

import issue_gov.receiver as receiver
import issue_gov.worker as worker
from common.errors import IssueProcessingError
from common.queueing import consume, dead_letter_count
from issue_gov.queues import ISSUE_ANALYSIS


REQUESTS = int(os.environ.get("CONCURRENCY_REQUESTS", "80"))
CONSUMERS = int(os.environ.get("CONCURRENCY_CONSUMERS", "4"))


def prepare_database() -> None:
    """使用独立的验证库，避免清空正式任务。"""
    import psycopg
    from psycopg import sql

    url = os.environ["DATABASE_URL"].split("?", 1)[0]
    name = url.rsplit("/", 1)[-1]
    if not name.replace("_", "").isalnum():
        print("验证库名只能包含字母、数字和下划线。", file=sys.stderr)
        raise SystemExit(1)
    admin = url.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as connection:
            exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            if exists is None:
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.Error as exc:
        print(f"数据库不可用：{exc.__class__.__name__}。请先执行 scripts/service compose up -d postgres rabbitmq", file=sys.stderr)
        raise SystemExit(1) from None


def assert_queue_idle() -> None:
    """拒绝在已有正式消费者时清空队列。验证开始前清掉残留消息。"""
    import pika
    from common.queueing import _parameters, declare

    connection = pika.BlockingConnection(_parameters(os.environ["RABBITMQ_URL"]))
    try:
        channel = connection.channel()
        declare(channel, ISSUE_ANALYSIS)
        current = channel.queue_declare(queue=ISSUE_ANALYSIS.analysis, durable=True, passive=True)
        if current.method.consumer_count:
            print("分析队列上已有消费者。请先停止 consumer 容器，再运行本脚本。", file=sys.stderr)
            raise SystemExit(1)
        channel.queue_purge(ISSUE_ANALYSIS.analysis)
        channel.queue_purge(ISSUE_ANALYSIS.dead_letter)
    finally:
        connection.close()


def build_config() -> receiver.Config:
    """构造只用于本次验证的配置，仓库名单不读取 runtime 文件。"""
    return receiver.Config(
        "concurrency-secret", "token", ("example/load",),
        str(ROOT / "issue-gov-agent" / "runtime" / "concurrency.sqlite3"),
        agent_api_key="model-key", llm_base_url="https://models.example", llm_model="model",
        database_url=os.environ["DATABASE_URL"], rabbitmq_url=os.environ["RABBITMQ_URL"],
        agent_api_keys=("model-key",), llm_failover=True,
    )


def post(base: str, number: int) -> int:
    """发送一条新建 Issue 通知，返回 HTTP 状态码。"""
    payload = {"object_kind": "issue", "uuid": f"load-{number}",
               "project": {"path_with_namespace": "example/load"},
               "object_attributes": {"action": "open", "iid": number}}
    request = urllib.request.Request(
        base + "/webhook", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-GitCode-Token": "concurrency-secret"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> None:
    prepare_database()
    assert_queue_idle()
    config = build_config()
    try:
        receiver.initialize(config)
    except receiver.DatabaseError as exc:
        print(f"数据库不可用：{exc}。请先执行 scripts/service compose up -d postgres rabbitmq", file=sys.stderr)
        raise SystemExit(1) from None
    with receiver.connect(config) as db:
        db.execute("DELETE FROM reports")
        db.execute("DELETE FROM events")

    processed: list[int] = []
    lock = threading.Lock()

    def fake_process(config, repository, number, *, client, checks=None):
        time.sleep(0.01)
        with lock:
            processed.append(number)
        if number == 900001:
            raise IssueProcessingError("全部模型密钥均调用失败", retryable=False, keys_exhausted=True)
        return {"status": "pass", "comment_action": "suppressed"}

    worker._process_issue = fake_process
    stop = threading.Event()
    errors: list[BaseException] = []

    def run_consumer() -> None:
        try:
            consume(config.rabbitmq_url, lambda payload, redelivered: worker.handle_delivery(
                config, payload, redelivered, client=object()), stop, ISSUE_ANALYSIS)
        except Exception as exc:
            errors.append(exc)
            stop.set()

    pump = threading.Thread(target=worker._pump, args=(config, stop), name="retry-pump", daemon=True)
    pump.start()
    threads = [threading.Thread(target=run_consumer, name=f"consumer-{index}", daemon=True)
               for index in range(CONSUMERS)]
    for thread in threads:
        thread.start()

    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, receiver.create_app(config), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    before_dead = dead_letter_count(config.rabbitmq_url, ISSUE_ANALYSIS)

    with ThreadPoolExecutor(max_workers=CONSUMERS * 4) as pool:
        statuses = list(pool.map(lambda number: post(base, number), range(1, REQUESTS + 1)))
    if statuses.count(202) != REQUESTS:
        print(f"接收入队结果异常：{statuses.count(202)}/{REQUESTS} 返回 202", file=sys.stderr)
        stop.set()
        raise SystemExit(1)

    deadline = time.time() + 60
    while time.time() < deadline:
        with receiver.connect(config) as db:
            done = db.execute("SELECT COUNT(*) FROM events WHERE state='completed'").fetchone()[0]
        if done == REQUESTS:
            break
        time.sleep(0.1)
    else:
        with receiver.connect(config) as db:
            summary = [dict(row) for row in db.execute(
                "SELECT state, COUNT(*) AS count FROM events GROUP BY state")]
        print(f"任务未在时限内完成：{summary}", file=sys.stderr)
        stop.set()
        raise SystemExit(1)

    post(base, 900001)
    deadline = time.time() + 20
    dead_row = None
    while time.time() < deadline:
        with receiver.connect(config) as db:
            dead_row = db.execute(
                "SELECT state, dead_lettered FROM events WHERE issue_number=900001").fetchone()
        if dead_row is not None and dead_row["state"] == "failed" and dead_row["dead_lettered"] == 1:
            break
        time.sleep(0.1)
    else:
        print(f"死信任务状态异常：{dict(dead_row) if dead_row else None}", file=sys.stderr)
        stop.set()
        raise SystemExit(1)

    deadline = time.time() + 20
    while time.time() < deadline:
        if dead_letter_count(config.rabbitmq_url, ISSUE_ANALYSIS) >= before_dead + 1:
            break
        time.sleep(0.2)
    else:
        print("数据库已标记失败，但死信队列没有新消息", file=sys.stderr)
        stop.set()
        raise SystemExit(1)

    stop.set()
    server.shutdown()
    duplicates = [number for number in processed if processed.count(number) > 1 and number != 900001]
    if duplicates or len([number for number in processed if number != 900001]) != REQUESTS:
        print(f"处理次数异常：完成 {len(processed)}，重复 {sorted(set(duplicates))}", file=sys.stderr)
        raise SystemExit(1)
    if errors:
        print(f"消费者异常：{errors!r}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({
        "requests": REQUESTS,
        "consumers": CONSUMERS,
        "processed_once": REQUESTS,
        "dead_lettered": 1,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
