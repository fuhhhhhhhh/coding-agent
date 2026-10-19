"""以独立预留身份结算主请求、摘要与重试的共享预算。"""

from dataclasses import dataclass
from uuid import uuid4


class BudgetExceededError(RuntimeError):
    """表示控制器预算耗尽，不是远端网络或认证故障。"""


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """区分实际用量与尚未确认的预留，未知成本不按零释放。"""

    requests: int
    input_tokens: int
    output_tokens: int
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    unknown_requests: int = 0


class BudgetLedger:
    """同步预留无需让出协程，按请求 ID 结算允许响应乱序返回。"""

    def __init__(
        self,
        *,
        max_requests: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        """初始化额度与逐请求预留；所有预算必须为正数。"""
        # 非正额度通常表示错误配置，不能解释成无限预算。
        if max_requests < 1 or any(
            value is not None and value < 1 for value in (max_input_tokens, max_output_tokens)
        ):
            raise ValueError("budget limits must be positive")
        self.max_requests = max_requests
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._reservations: dict[str, tuple[int, int]] = {}

    def reserve_request(self, estimated_input_tokens: int = 0, output_tokens: int = 0) -> str:
        """在网络前同时预留输入和最大输出，返回唯一身份。"""
        # 负预留会反向扩大可用额度，必须在更改状态前拒绝。
        if estimated_input_tokens < 0 or output_tokens < 0:
            raise ValueError("negative token reservation")
        snapshot = self.snapshot
        # 次数、实际用量与全部并发预留共同构成上限，不能只看已结算部分。
        if snapshot.requests >= self.max_requests:
            raise BudgetExceededError("provider request budget exhausted")
        if self.max_input_tokens is not None and (
            snapshot.input_tokens + snapshot.reserved_input_tokens + estimated_input_tokens
            > self.max_input_tokens
        ):
            raise BudgetExceededError("provider input token budget exhausted")
        if self.max_output_tokens is not None and (
            snapshot.output_tokens + snapshot.reserved_output_tokens + output_tokens
            > self.max_output_tokens
        ):
            raise BudgetExceededError("provider output token budget exhausted")
        identity = uuid4().hex
        self._reservations[identity] = (estimated_input_tokens, output_tokens)
        self._requests += 1
        return identity

    def settle(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        reservation_id: str | None = None,
    ) -> None:
        """结算指定请求；未计量字段保留预留，重复或未知身份拒绝。"""
        if reservation_id is None:
            # 旧调用仅在恰好一个待结算请求时兼容，多个请求绝不能用 FIFO 猜测归属。
            if len(self._reservations) != 1:
                raise ValueError("reservation identity required")
            reservation_id = next(iter(self._reservations))
        # 未知或已经完全结算的身份不能重复记账。
        if reservation_id not in self._reservations:
            raise ValueError("unknown or settled reservation")
        old_input, old_output = self._reservations[reservation_id]
        self._input_tokens += max(input_tokens, 0) if input_tokens is not None else 0
        self._output_tokens += max(output_tokens, 0) if output_tokens is not None else 0
        # 仅完全计量才移除预留；部分未知字段继续占用原估算。
        if input_tokens is not None and output_tokens is not None:
            del self._reservations[reservation_id]
        else:
            self._reservations[reservation_id] = (
                old_input if input_tokens is None else 0,
                old_output if output_tokens is None else 0,
            )
        # 已发生的超报仍须记入账本，再阻止后续调用；不能丢掉服务端计量事实。
        snapshot = self.snapshot
        if self.max_input_tokens is not None and (
            snapshot.input_tokens + snapshot.reserved_input_tokens > self.max_input_tokens
        ):
            raise BudgetExceededError("provider input token budget exhausted")
        if self.max_output_tokens is not None and (
            snapshot.output_tokens + snapshot.reserved_output_tokens > self.max_output_tokens
        ):
            raise BudgetExceededError("provider output token budget exhausted")

    @property
    def snapshot(self) -> BudgetSnapshot:
        """返回实际与未确认占用，供持久化和终端只读展示。"""
        return BudgetSnapshot(
            self._requests,
            self._input_tokens,
            self._output_tokens,
            sum(value[0] for value in self._reservations.values()),
            sum(value[1] for value in self._reservations.values()),
            len(self._reservations),
        )
