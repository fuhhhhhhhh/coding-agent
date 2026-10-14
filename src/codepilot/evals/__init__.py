"""提供本地评测任务、独立验证器和批量 Harness。"""

from codepilot.evals.harness import LocalHarness, ProviderFactory
from codepilot.evals.models import EvalStatus, TaskSpec, TrialResult, VerificationResult
from codepilot.evals.verifier import IndependentVerifier, IsolatedVerifier

__all__ = [
    "EvalStatus",
    "IndependentVerifier",
    "IsolatedVerifier",
    "LocalHarness",
    "ProviderFactory",
    "TaskSpec",
    "TrialResult",
    "VerificationResult",
]
