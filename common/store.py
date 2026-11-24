"""SQLite 与 PostgreSQL 的统一存储入口。

调用方提供 database（本机 SQLite 路径）和 database_url（服务模式下的 PostgreSQL 连接串）。
database_url 非空时使用 PostgreSQL，否则使用 SQLite。本模块不依赖具体治理服务。
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Protocol

from .pgstore import DatabaseError, connect as pg_connect, initialize as pg_initialize
from .queueing import redact_url
from .schema import apply_sqlite


class DatabaseLocation(Protocol):
    """能打开任务库的配置。治理服务把自己的配置对象传进来即可。"""

    database: str
    database_url: str


@contextmanager
def connect(location: DatabaseLocation) -> Iterator[Any]:
    """创建一次数据库会话，退出时提交或回滚事务，并关闭连接。

    Args:
        location: 含 SQLite 路径和可选 PostgreSQL 连接串的配置。
    Yields:
        SQLite 连接或 PostgreSQL 包装连接，查询结果都可按列名访问。
    """
    if location.database_url:
        with pg_connect(location.database_url) as db:
            yield db
        return
    db = sqlite3.connect(location.database, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(location: DatabaseLocation) -> None:
    """创建目录、表和索引，并把被更新请求替代的旧任务标为已覆盖。可以重复调用。"""
    if location.database_url:
        pg_initialize(location.database_url)
        return
    Path(location.database).parent.mkdir(parents=True, exist_ok=True)
    with connect(location) as db:
        db.execute("BEGIN IMMEDIATE")
        apply_sqlite(db)


def queue_snapshot(location: DatabaseLocation) -> dict[str, Any]:
    """只读查询各仓库的任务统计和最近五条任务，不创建或修改数据库。

    Returns:
        database 为 SQLite 绝对路径，或去掉密码的 PostgreSQL 连接串。
        exists 表示库可读取；counts 为按仓库及状态分组的数量，recent 为最近五条任务。
    """
    if location.database_url:
        snapshot = {"database": redact_url(location.database_url), "exists": True,
                    "counts": [], "recent": []}
        try:
            with connect(location) as db:
                snapshot["counts"] = [dict(row) for row in db.execute(
                    "SELECT repository, state, COUNT(*) AS count FROM events "
                    "GROUP BY repository, state ORDER BY repository, state").fetchall()]
                snapshot["recent"] = [dict(row) for row in db.execute(
                    "SELECT repository, issue_number, state, attempts, updated_at, last_error, result "
                    "FROM events ORDER BY updated_at DESC, event_id LIMIT 5").fetchall()]
        except DatabaseError:
            snapshot["exists"] = False
        return snapshot
    path = Path(location.database).resolve()
    snapshot: dict[str, Any] = {"database": str(path), "exists": path.is_file(),
                                "counts": [], "recent": []}
    if not snapshot["exists"]:
        return snapshot
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("BEGIN")
            snapshot["counts"] = [dict(row) for row in db.execute(
                "SELECT repository, state, COUNT(*) AS count FROM events "
                "GROUP BY repository, state ORDER BY repository, state")]
            snapshot["recent"] = [dict(row) for row in db.execute(
                "SELECT repository, issue_number, state, attempts, updated_at, last_error, result "
                "FROM events ORDER BY updated_at DESC, event_id LIMIT 5")]
    finally:
        db.close()
    return snapshot
