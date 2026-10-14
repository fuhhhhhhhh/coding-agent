"""把仓库规则和任务信息组装为受保护的初始上下文消息。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts import Message
from codepilot.contracts.models import Contract


class RepositoryRule(Contract):
    """保存一份仓库规则的来源标签和完整正文。"""

    source: Annotated[str, Field(min_length=1)]
    content: Annotated[str, Field(min_length=1)]


class ContextSeed(Contract):
    """保存一次新任务必须长期保留的规则与任务信息。"""

    repository_rules: tuple[RepositoryRule, ...] = ()
    task_information: Annotated[str, Field(min_length=1)]


def assemble_initial_context(seed: ContextSeed) -> tuple[Message, ...]:
    """把每份仓库规则放入独立 system 消息，并把任务放入 user 消息。

    ContextManager 会把运行开始时连续的 system/user 消息视为受保护前缀，因此自动
    压缩不会删除这些规则和任务。相关代码文件不在这里预加载；模型继续通过现有
    glob/search/read_file 工具按需定位，避免把整个仓库提前塞入 prompt。
    """
    rules = tuple(
        Message(role="system", content=f"[仓库规则：{rule.source}]\n{rule.content}")
        for rule in seed.repository_rules
    )
    return (*rules, Message(role="user", content=f"[任务信息]\n{seed.task_information}"))
