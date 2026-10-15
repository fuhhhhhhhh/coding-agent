"""提供统一 ``codepilot run`` 命令，并把所有执行委托给产品装配层。"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from codepilot.app import CodePilotApplication, RunIdentity
from codepilot.config import ProductConfig, load_product_config
from codepilot.ui.renderer import EventRenderer


def build_parser() -> argparse.ArgumentParser:
    """构造无隐式网络行为的命令行解析器。"""
    parser = argparse.ArgumentParser(prog="codepilot", description="CodePilot 终端 Coding Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="在一个工作区启动新任务")
    run.add_argument("task", help="需要 Agent 完成的任务")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--config", type=Path, help="最高优先级 TOML 配置文件")
    run.add_argument("--provider", choices=("fake", "openai-compatible"))
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument("--api-key-env")
    run.add_argument("--backend", choices=("local", "docker"))
    run.add_argument("--docker-image")
    run.add_argument("--permission", choices=("read-only", "ask-edits", "controlled-edit"))
    run.add_argument("--state-directory", type=Path)
    run.add_argument("--session-id")
    run.add_argument("--task-id")
    run.add_argument("--run-id")
    run.add_argument("--jsonl", action="store_true", help="输出脱敏进度事件和最终 JSON")
    return parser


def _overrides(arguments: argparse.Namespace) -> dict[str, object]:
    """只收集用户明确传入的 CLI 值，使未指定选项不覆盖文件配置。"""
    result: dict[str, object] = {}
    provider = {
        key: value
        for key, value in {
            "kind": arguments.provider,
            "model": arguments.model,
            "base_url": arguments.base_url,
            "api_key_env": arguments.api_key_env,
        }.items()
        if value is not None
    }
    backend = {
        key: value
        for key, value in {
            "kind": arguments.backend,
            "docker_image": arguments.docker_image,
        }.items()
        if value is not None
    }
    if provider:
        result["provider"] = provider
    if backend:
        result["backend"] = backend
    if arguments.permission is not None:
        result["permission"] = arguments.permission
    if arguments.state_directory is not None:
        result["state_directory"] = arguments.state_directory
    return result


async def _run(arguments: argparse.Namespace, config: ProductConfig) -> int:
    """建立渲染器和产品应用，并把 Runtime 终态映射为进程退出码。"""
    if not arguments.task.strip():
        raise ValueError("task must not be empty")
    renderer = EventRenderer(sys.stdout, jsonl=arguments.jsonl)
    application = CodePilotApplication(
        config,
        arguments.workspace,
        on_event=renderer,
    )
    # CLI 在副作用发生前生成真实身份，使第一条展示与 Trace 使用完全相同的 ID。
    identity = RunIdentity(
        session_id=arguments.session_id or f"session-{uuid.uuid4().hex}",
        task_id=arguments.task_id or f"task-{uuid.uuid4().hex}",
        run_id=arguments.run_id or f"run-{uuid.uuid4().hex}",
    )
    renderer.started(identity, config.backend.kind, config.provider.kind)
    result = await application.run(
        arguments.task,
        session_id=identity.session_id,
        task_id=identity.task_id,
        run_id=identity.run_id,
    )
    renderer.finished(result)
    return 0 if result.report.reason == "completed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    """解析配置后运行唯一产品入口，并为输入错误和中断返回稳定退出码。"""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        workspace = arguments.workspace.resolve(strict=True)
        config = load_product_config(
            workspace=workspace,
            explicit_path=arguments.config,
            overrides=_overrides(arguments),
        )
        return asyncio.run(_run(arguments, config))
    except KeyboardInterrupt:
        # asyncio.run 会先取消主任务；此处只负责稳定的终端退出码。
        print("运行已取消。", file=sys.stderr)
        return 130
    except (FileNotFoundError, NotADirectoryError, ValidationError, ValueError) as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    except Exception as error:
        # 未知启动错误只展示类型，避免 SDK 异常正文携带请求或凭据。
        print(f"启动失败：{type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
