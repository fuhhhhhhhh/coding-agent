"""定义脱敏错误分类和仅在明确未接受请求时使用的有界退避。"""

import math
import time
from email.utils import parsedate_to_datetime
from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract


class RetryPolicy(Contract):
    """限制单个逻辑请求的尝试次数与退避时长，SDK 自动重试仍关闭。"""

    max_attempts: Annotated[int, Field(ge=1, le=5)] = 3
    base_delay_seconds: Annotated[float, Field(ge=0, le=30)] = 1.0
    max_delay_seconds: Annotated[float, Field(ge=0, le=120)] = 30.0


def error_category(error: Exception) -> str:
    """只根据异常类型和状态码分类，不读取或回显 SDK 错误正文。"""
    status = getattr(error, "status_code", None)
    name = type(error).__name__
    if status in {401, 403} or name in {"AuthenticationError", "PermissionDeniedError"}:
        return "authentication"
    if status == 402:
        return "balance"
    if status == 429:
        return "rate_limit"
    if name in {"APITimeoutError", "TimeoutError"}:
        return "timeout_unknown"
    if name == "APIConnectionError":
        return "connection_unknown"
    if isinstance(status, int) and status >= 500:
        return "server_unknown"
    if status == 400:
        return "invalid_request"
    return "provider_unknown"


def retry_delay(error: Exception, attempt: int, policy: RetryPolicy) -> float | None:
    """仅为明确拒绝或建连失败给出退避；过长 Retry-After 直接停止。"""
    # 尝试耗尽时不再等待，避免制造看似卡住但不会发送请求的退避。
    if attempt >= policy.max_attempts:
        return None
    status = getattr(error, "status_code", None)
    cause = error.__cause__
    # httpx 的建连错误表示 HTTP 请求尚未发出；读写超时与断流不满足此条件。
    connect_failed = type(error).__name__ in {"APIConnectionError", "APITimeoutError"} and type(
        cause
    ).__name__ in {
        "ConnectError",
        "ConnectTimeout",
    }
    # 服务端接受状态未知时，重发可能重复收费或产生另一份工具计划。
    if status != 429 and not connect_failed:
        return None
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    raw = headers.get("retry-after")
    delay = min(policy.base_delay_seconds * 2 ** (attempt - 1), policy.max_delay_seconds)
    # 服务器明确要求的等待优先于本地指数退避，但不突破任务等待边界。
    if raw is not None:
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            # Retry-After 也可以是标准 HTTP 日期，不能只支持秒数。
            try:
                delay = parsedate_to_datetime(raw).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                # 无法理解的等待值保持停止，不能猜测立即重放。
                return None
        # 不把服务器要求的长等待偷偷缩短，否则会提前重放被限流的请求。
        if not math.isfinite(delay) or delay > policy.max_delay_seconds or delay < 0:
            return None
    return float(delay)
