"""实现跨主请求和摘要请求共享的有界预算账本。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """描述预算账本当前已预留和已结算的资源。"""

    requests: int
    input_tokens: int
    output_tokens: int


class BudgetLedger:
    """以预留优先的方式限制请求数和已知 Token 用量。"""

    def __init__(
        self,
        *,
        max_requests: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        """创建共享账本；未知用量不会被当成零消耗释放。"""
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.max_requests = max_requests
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._reserved_inputs: list[int] = []

    def reserve_request(self, estimated_input_tokens: int = 0) -> None:
        """在网络请求前预留请求和已知输入额度，超限时不接触 Provider。"""
        if self._requests >= self.max_requests:
            raise RuntimeError("provider request budget exhausted")
        if (
            self.max_input_tokens is not None
            and self._input_tokens + sum(self._reserved_inputs) + estimated_input_tokens
            > self.max_input_tokens
        ):
            raise RuntimeError("provider input token budget exhausted")
        self._requests += 1
        self._reserved_inputs.append(max(estimated_input_tokens, 0))

    def settle(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """结算服务端 usage；缺失 usage 保持已预留额度，不释放不确定成本。"""
        if self._reserved_inputs:
            self._reserved_inputs.pop(0)
        if input_tokens is not None:
            next_total = self._input_tokens + max(input_tokens, 0)
            if self.max_input_tokens is not None and next_total > self.max_input_tokens:
                raise RuntimeError("provider input token budget exhausted")
            self._input_tokens = next_total
        if output_tokens is not None:
            next_total = self._output_tokens + max(output_tokens, 0)
            if self.max_output_tokens is not None and next_total > self.max_output_tokens:
                raise RuntimeError("provider output token budget exhausted")
            self._output_tokens = next_total

    @property
    def snapshot(self) -> BudgetSnapshot:
        """返回不可变的账本快照，便于 Trace 和测试独立核对。"""
        return BudgetSnapshot(self._requests, self._input_tokens, self._output_tokens)
