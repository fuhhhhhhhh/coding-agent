"""验证离线配置的默认值、显式读取和错误输入处理。"""

import tomllib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from codepilot.config import Config, ProductConfig, load_config, load_product_config


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


def test_product_config_layers_have_stable_precedence(tmp_path: Path) -> None:
    """用户、项目、显式文件和命令行覆盖必须按声明顺序逐层合并。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user = tmp_path / "user.toml"
    explicit = tmp_path / "explicit.toml"
    user.write_text('[runtime]\nmax_steps = 3\n[provider]\nmodel = "fake-user"\n')
    (workspace / ".codepilot.toml").write_text(
        '[runtime]\nmax_steps = 4\n[provider]\nmodel = "fake-project"\n'
    )
    explicit.write_text('[runtime]\nmax_tool_calls = 7\n[provider]\nmodel = "fake-explicit"\n')

    config = load_product_config(
        workspace=workspace,
        user_path=user,
        explicit_path=explicit,
        overrides={"runtime": {"max_steps": 5}},
    )

    assert config.provider.model == "fake-explicit"
    assert config.runtime.max_steps == 5
    assert config.runtime.max_tool_calls == 7


def test_product_yaml_is_discovered_and_cli_still_has_highest_priority(tmp_path: Path) -> None:
    """工作区 .codepilot/config.yaml 应生效，显式覆盖仍拥有最高优先级。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_config = workspace / ".codepilot" / "config.yaml"
    project_config.parent.mkdir()
    project_config.write_text(
        "provider:\n  model: fake-yaml\nruntime:\n  max_steps: 6\n",
        encoding="utf-8",
    )

    config = load_product_config(
        workspace=workspace,
        user_path=tmp_path / "absent",
        overrides={"runtime": {"max_steps": 7}},
    )

    assert config.provider.model == "fake-yaml"
    assert config.runtime.max_steps == 7


def test_project_config_cannot_change_provider_or_backend_security(tmp_path: Path) -> None:
    """工作区配置只能调整任务参数，不能切换网关、凭据来源或执行后端。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_config = workspace / ".codepilot.toml"
    project_config.write_text('[provider]\nbase_url = "https://evil.invalid"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Provider 安全字段"):
        load_product_config(workspace=workspace, user_path=tmp_path / "absent")

    project_config.write_text('[backend]\nkind = "local"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="执行后端字段"):
        load_product_config(workspace=workspace, user_path=tmp_path / "absent")


def test_project_config_can_only_tighten_permission(tmp_path: Path) -> None:
    """工作区可以要求更严格审批，但不能把用户策略改成更宽松模式。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user = tmp_path / "user.toml"
    user.write_text('permission = "ask-edits"\n')
    project_config = workspace / ".codepilot.toml"
    project_config.write_text('permission = "read-only"\n', encoding="utf-8")
    assert load_product_config(workspace=workspace, user_path=user).permission == "read-only"
    project_config.write_text('permission = "controlled-edit"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="扩大用户权限"):
        load_product_config(workspace=workspace, user_path=user)


@pytest.mark.parametrize("content", ["provider: [", "- first\n- second\n"])
def test_product_yaml_rejects_malformed_or_non_mapping_root(tmp_path: Path, content: str) -> None:
    """损坏的 YAML 和非对象顶层都必须在创建运行资源前失败。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_config = workspace / ".codepilot" / "config.yaml"
    project_config.parent.mkdir()
    project_config.write_text(content, encoding="utf-8")

    with pytest.raises((yaml.YAMLError, ValueError)):
        load_product_config(workspace=workspace, user_path=tmp_path / "absent")


def test_product_config_rejects_ambiguous_project_files(tmp_path: Path) -> None:
    """同一层有多个候选配置时应拒绝静默选择，防止用户改错文件。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_config = workspace / ".codepilot" / "config.yaml"
    project_config.parent.mkdir()
    project_config.write_text("{}\n", encoding="utf-8")
    (workspace / ".codepilot.toml").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="多个 CodePilot 配置文件"):
        load_product_config(workspace=workspace, user_path=tmp_path / "absent")


def test_product_config_rejects_secret_and_impossible_backend(tmp_path: Path) -> None:
    """配置文件不能保存密钥，也不能选择没有镜像的 Docker 后端。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = workspace / ".codepilot.toml"
    secret.write_text('[provider]\napi_key = "must-not-be-stored"\n')
    with pytest.raises(ValidationError):
        load_product_config(workspace=workspace, user_path=tmp_path / "absent")

    secret.write_text('[backend]\nkind = "docker"\n')
    with pytest.raises((ValidationError, ValueError)):
        load_product_config(workspace=workspace, user_path=tmp_path / "absent")


def test_real_provider_requires_compatible_url() -> None:
    """联网 Provider 必须显式声明网关，fake 配置不能携带真实模型名。"""
    with pytest.raises(ValidationError):
        ProductConfig.model_validate({"provider": {"kind": "openai-compatible", "model": "qwen"}})
    with pytest.raises(ValidationError):
        ProductConfig.model_validate({"provider": {"model": "qwen"}})


def test_qwen_scale_context_and_output_limits_are_accepted() -> None:
    """产品配置应容纳 1M 总上下文和 Qwen 支持的 64K 最大输出。"""
    config = ProductConfig.model_validate(
        {
            "provider": {"max_tokens": 65_536},
            "context": {
                "max_prompt_tokens": 1_000_000,
                "response_reserve_tokens": 65_536,
                "compact_trigger_tokens": 850_000,
                "compact_target_tokens": 700_000,
            },
        }
    )

    assert config.provider.max_tokens == 65_536
    assert config.context.usable_prompt_tokens == 934_464


def test_output_limit_above_supported_model_maximum_is_rejected() -> None:
    """超过当前 Provider 边界的输出值应在网络请求前被拒绝。"""
    with pytest.raises(ValidationError):
        ProductConfig.model_validate({"provider": {"max_tokens": 65_537}})


def test_yaml_style_git_verification_argv_becomes_immutable_tuple() -> None:
    """YAML 数组应在配置边界转成固定 argv，避免运行期间被修改。"""
    config = ProductConfig.model_validate(
        {"git_workspace": {"verification_command": ["python", "-m", "pytest", "-q"]}}
    )

    assert config.git_workspace.verification_command == ("python", "-m", "pytest", "-q")


def test_product_config_defaults_to_the_current_workspace() -> None:
    """产品默认值应直接使用当前目录，并允许目录处于未初始化或有改动状态。"""
    config = ProductConfig()

    assert config.git_workspace.enabled is False
    assert config.git_workspace.require_clean_source is False
