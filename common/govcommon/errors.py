"""各治理模块共用的安全错误信息与重试策略，不依赖具体平台或分析功能。"""


class ProcessingError(Exception):
    """可安全记录的治理错误；权限、配置问题不自动重试。"""

    def __init__(self, message: str, retryable: bool = True, *,
                 status_code: int | None = None, retry_after: float = 0,
                 keys_exhausted: bool = False) -> None:
        """记录经过脱敏的错误原因及重试策略。

        Args:
            message: 不包含令牌、完整请求 URL 或响应原文的错误说明。
            retryable: 网络或临时服务错误为 True；权限、参数等确定性错误为 False。
            status_code: 平台 HTTP 状态码，用于识别 429 限流。
            retry_after: 服务端要求的等待秒数。
            keys_exhausted: 全部模型密钥都已失败时为 True，任务进入死信队列并不再按任务级重试。
        Returns:
            None。
        """
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.keys_exhausted = keys_exhausted


# 现有 Issue 治理代码和测试沿用这个名字，它与 ProcessingError 是同一个类型。
IssueProcessingError = ProcessingError
