"""按 endpoint/model/protocol 提供有日期来源的能力边界与保守费用估算。"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from codepilot.config import ProviderConfig


SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
AS_OF = "2026-10-19"


def provider_profile(provider: "ProviderConfig", context_tokens: int) -> dict[str, Any]:
    """只认可准确官方组合，未知网关保持能力与价格未知而非套用模型名。"""
    url = urlsplit(provider.base_url or "")
    official = (
        url.scheme == "https"
        and url.hostname == "api.deepseek.com"
        and url.port in {None, 443}
        and url.path.rstrip("/") in {"", "/v1"}
    )
    prices = {"deepseek-flash": (0.3, 1.2), "deepseek-v4-pro": (1.32, 3.96)}
    known = official and provider.model in prices
    profile: dict[str, Any] = {
        "model": provider.model,
        "protocol": provider.protocol,
        "capability_source": SOURCE if known else "user_config_unverified",
        "as_of": AS_OF if known else None,
        "context_tokens": 1_000_000 if known else context_tokens,
        "adapter_max_output_tokens": 65_536,
        "tools": True if known else None,
        "stream": True if known else None,
        "usage": "observed_not_guaranteed" if known else "unknown",
        "reasoning": "not_retained_by_current_adapter",
        "real_verification": "B3 DeepSeek Flash two protocols"
        if known and provider.model == "deepseek-flash"
        else "not_run",
        "price_policy": "peak_cache_miss_upper_estimate; not_account_invoice"
        if known
        else "unknown",
        "input_usd_per_million": prices[provider.model][0] if known else None,
        "output_usd_per_million": prices[provider.model][1] if known else None,
    }
    # 服务文档能力与本地适配器上限取更小值，不能因服务扩容就冒称已支持。
    if (
        context_tokens > profile["context_tokens"]
        or provider.max_tokens > profile["adapter_max_output_tokens"]
    ):
        raise ValueError("configured context/output exceeds provider capability profile")
    return profile


def estimated_cost(input_tokens: int, output_tokens: int, prices: list[float]) -> str:
    """用十进制定价计算保守估算，价格和实际账户扣费仍分开描述。"""
    amount = (
        Decimal(input_tokens) * Decimal(str(prices[0]))
        + Decimal(output_tokens) * Decimal(str(prices[1]))
    ) / Decimal(1_000_000)
    return str(amount)
