"""服务根目录上的配置、静态资源和运行数据。这些路径不属于 Python 包。"""

from pathlib import Path


def service_root() -> Path:
    """issue-gov-agent 目录。容器内是挂载配置的工作目录。"""
    return Path(__file__).resolve().parents[1]


def env_file() -> Path:
    """唯一的环境文件，与 manage.py 同级，不放在 runtime 里。"""
    return service_root() / ".env"


def repository_file() -> Path:
    """接入仓库及维护者，与 .env 同级。"""
    return service_root() / "repository.yml"


def config_dir() -> Path:
    """随代码发布、但不写在 Python 模块里的检查资源。"""
    return service_root() / "config"


def runtime_dir() -> Path:
    """日志、SQLite 和本机进程套接字。"""
    return service_root() / "runtime"
