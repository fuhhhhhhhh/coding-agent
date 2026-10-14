"""公开上下文治理的预算、落盘、压缩和按需读取入口。"""

from codepilot.context.artifacts import ContextArtifactStore
from codepilot.context.assembly import ContextSeed, RepositoryRule, assemble_initial_context
from codepilot.context.manager import (
    ContextManager,
    ContextPreparationError,
    ReadContextArtifactArguments,
    estimate_message,
    estimate_messages,
)
from codepilot.context.models import (
    ArtifactReference,
    ArtifactSlice,
    ContextLimits,
    ContextRecord,
    ContextView,
    SummaryResult,
)
from codepilot.context.summarizer import ModelSummarizer, Summarizer

__all__ = [
    "ArtifactReference",
    "ArtifactSlice",
    "ContextArtifactStore",
    "ContextLimits",
    "ContextManager",
    "ContextPreparationError",
    "ContextRecord",
    "ContextSeed",
    "ContextView",
    "ModelSummarizer",
    "ReadContextArtifactArguments",
    "RepositoryRule",
    "Summarizer",
    "SummaryResult",
    "assemble_initial_context",
    "estimate_message",
    "estimate_messages",
]
