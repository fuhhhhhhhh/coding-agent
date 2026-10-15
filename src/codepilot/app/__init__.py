"""公开统一产品装配入口及其运行结果契约。"""

from codepilot.app.models import ProductRunResult, RunIdentity
from codepilot.app.service import CodePilotApplication

__all__ = ["CodePilotApplication", "ProductRunResult", "RunIdentity"]
