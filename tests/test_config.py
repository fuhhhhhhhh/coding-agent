"""验证离线配置的默认值、显式读取和错误输入处理。"""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.config import Config, load_config


def test_default_and_explicit_config(tmp_path: Path) -> None:
    """确保默认值与对应的显式 TOML 产生相同配置。"""
    path = tmp_path / "config.toml"
    path.write_text('provider = "fake"\nmodel = "fake-m01"\n', encoding="utf-8")
    assert load_config(path) == load_config() == Config()


@pytest.mark.parametrize(
    "content",
    [
        'provider = "openai"',
        'model = "real-model"',
        'api_key = "dummy"',
        "provider = 1",
    ],
)
def test_invalid_configuration(tmp_path: Path, content: str) -> None:
    """拒绝不支持的 Provider、模型、配置键和字段类型。"""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_missing_file_and_malformed_toml(tmp_path: Path) -> None:
    """对显式路径保留文件系统错误与 TOML 解析错误的区别。"""
    path = tmp_path / "missing.toml"
    with pytest.raises(FileNotFoundError):
        load_config(path)
    path.write_text('provider = "unterminated', encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(path)
