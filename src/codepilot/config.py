"""显式 TOML 配置；不隐式读取环境变量或凭据。"""

import tomllib
from pathlib import Path
from typing import Literal

from codepilot.contracts.models import Contract


class Config(Contract):
    """保存 M01 仅支持 fake Provider 的最小离线配置。"""

    provider: Literal["fake"] = "fake"
    model: Literal["fake-m01"] = "fake-m01"


def load_config(path: Path | None = None) -> Config:
    """读取并校验 ``path`` 指向的 TOML；未提供路径时返回离线默认配置。"""
    # 未传路径表示使用默认值；显式传入的错误路径必须原样暴露给调用方。
    if path is None:
        return Config()
    with path.open("rb") as file:
        return Config.model_validate(tomllib.load(file))
