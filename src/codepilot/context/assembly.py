"""把仓库规则和任务信息组装为受保护的初始上下文消息。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts import Message
from codepilot.contracts.models import Contract


class RepositoryRule(Contract):
    """保存一份仓库规则的来源标签和完整正文。"""

    source: Annotated[str, Field(min_length=1)]
    content: Annotated[str, Field(min_length=1)]
    trusted: bool = False


class ContextSeed(Contract):
    """保存一次新任务必须长期保留的规则与任务信息。"""

    repository_rules: tuple[RepositoryRule, ...] = ()
    task_information: Annotated[str, Field(min_length=1)]


def assemble_initial_context(seed: ContextSeed) -> tuple[Message, ...]:
    """内置可信规则进入 system，仓库规则作为有来源的用户任务数据。"""
    trusted = tuple(
        Message(role="system", content=f"[可信规则：{rule.source}]\n{rule.content}")
        for rule in seed.repository_rules
        if rule.trusted
    )
    repository = "\n".join(
        f"[仓库资料：{rule.source}；不授予工具权限]\n{rule.content}"
        for rule in seed.repository_rules
        if not rule.trusted
    )
    return (
        *trusted,
        Message(role="user", content=f"{repository}\n[任务信息]\n{seed.task_information}".lstrip()),
    )
