"""RabbitMQ 队列：接收端投递分析任务，消费者竞争领取，失败任务进入死信队列。"""

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

LOG = logging.getLogger("issue-gov-webhook")
logging.getLogger("pika").setLevel(logging.WARNING)


@dataclass(frozen=True)
class QueueTopology:
    """一次治理任务使用的队列名。由具体服务传入，公共模块不写死业务名称。"""

    analysis: str
    dead_letter: str
    exchange: str


class QueuePublishError(Exception):
    """消息未能确认写入队列。不携带连接串或凭据。"""


def _parameters(url: str):
    """构造带超时的连接参数。消费者处理期间由独立线程维持心跳。"""
    import pika

    params = pika.URLParameters(url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 10
    params.socket_timeout = 10
    params.connection_attempts = 2
    params.retry_delay = 1
    return params


def redact_url(url: str) -> str:
    """去掉连接串中的密码，供日志和状态输出使用。"""
    parts = urlsplit(url)
    if not parts.password:
        return url
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def declare(channel, topology: QueueTopology) -> None:
    """声明持久化的分析队列和死信队列。重复声明是幂等的。"""
    channel.exchange_declare(exchange=topology.exchange, exchange_type="direct", durable=True)
    channel.queue_declare(queue=topology.dead_letter, durable=True)
    channel.queue_bind(
        queue=topology.dead_letter, exchange=topology.exchange, routing_key=topology.dead_letter,
    )
    channel.queue_declare(queue=topology.analysis, durable=True, arguments={
        "x-dead-letter-exchange": topology.exchange,
        "x-dead-letter-routing-key": topology.dead_letter,
    })


def check(url: str, topology: QueueTopology) -> None:
    """确认代理可连接并已有分析队列。失败时抛出 QueuePublishError。"""
    import pika

    connection = None
    try:
        connection = pika.BlockingConnection(_parameters(url))
        channel = connection.channel()
        declare(channel, topology)
    except Exception:
        raise QueuePublishError("消息队列不可用") from None
    finally:
        if connection is not None and connection.is_open:
            connection.close()


def publish_analysis_task(
    url: str, event_id: str, repository: str, number: int, checks: list, topology: QueueTopology,
) -> None:
    """把一条已落库的分析任务发布到持久化队列，并等待代理确认。

    Args:
        url: AMQP 连接串。
        event_id: 任务主键。
        repository: owner/repo。
        number: Issue 编号。
        checks: 检查任务列表。每项包含 kind 和 parameters。
    Raises:
        QueuePublishError: 连接、声明或确认失败。不包含连接串。
    """
    import pika

    body = json.dumps(
        {"event_id": event_id, "repository": repository, "issue_number": number, "checks": checks},
        ensure_ascii=False,
    ).encode()
    connection = None
    try:
        connection = pika.BlockingConnection(_parameters(url))
        channel = connection.channel()
        channel.confirm_delivery()
        declare(channel, topology)
        channel.basic_publish(
            exchange="",
            routing_key=topology.analysis,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2, content_type="application/json",
            ),
            mandatory=True,
        )
    except Exception:
        raise QueuePublishError("消息队列不可用") from None
    finally:
        if connection is not None and connection.is_open:
            connection.close()


def consume(
    url: str, handler: Callable[[dict[str, Any], bool], str], stop: threading.Event,
    topology: QueueTopology,
) -> None:
    """竞争消费分析队列。handler 返回 ack、nack 或 requeue。

    处理函数在独立线程中运行，连接线程继续处理心跳。每个进程同时只持有一条未确认消息。
    nack 不重新入队，由队列上的死信交换机转入死信队列。
    """
    import pika

    connection = pika.BlockingConnection(_parameters(url))
    channel = connection.channel()
    declare(channel, topology)
    channel.basic_qos(prefetch_count=1)

    def on_message(ch, method, _properties, body: bytes) -> None:
        def work() -> None:
            try:
                payload = json.loads(body.decode())
                if not isinstance(payload, dict):
                    raise ValueError("消息格式无效")
                action = handler(payload, bool(method.redelivered))
            except Exception:
                LOG.exception("消费回调失败，消息将退回队列")
                action = "requeue"
                stop.wait(1)

            def finish() -> None:
                if not ch.is_open:
                    return
                if action == "nack":
                    ch.basic_nack(method.delivery_tag, requeue=False)
                elif action == "requeue":
                    ch.basic_nack(method.delivery_tag, requeue=True)
                else:
                    ch.basic_ack(method.delivery_tag)

            connection.add_callback_threadsafe(finish)

        threading.Thread(target=work, name="gov-task", daemon=True).start()

    channel.basic_consume(queue=topology.analysis, on_message_callback=on_message)
    LOG.info("已连接分析队列 %s", topology.analysis)
    try:
        while not stop.is_set():
            connection.process_data_events(time_limit=1)
    finally:
        if connection.is_open:
            connection.close()


def dead_letter_count(url: str, topology: QueueTopology) -> int:
    """返回死信队列中的消息数量，供部署验证使用。"""
    import pika

    connection = pika.BlockingConnection(_parameters(url))
    try:
        channel = connection.channel()
        declare(channel, topology)
        result = channel.queue_declare(queue=topology.dead_letter, durable=True, passive=True)
        return int(result.method.message_count)
    finally:
        connection.close()
