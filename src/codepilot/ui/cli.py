"""提供交互式 ``codepilot`` 与单次 ``codepilot run`` 产品入口。"""

import argparse
import asyncio
import json
import shutil
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from codepilot.app import CodePilotApplication, RunIdentity
from codepilot.app.service import _state_root
from codepilot.config import ProductConfig, load_product_config
from codepilot.sessions.store import SessionStore
from codepilot.ui.renderer import EventRenderer


def build_parser() -> argparse.ArgumentParser:
    """构造无隐式网络行为的命令行解析器。"""
    parser = argparse.ArgumentParser(prog="codepilot", description="CodePilot 终端 Coding Agent")
    _add_shared_arguments(parser)
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="在一个工作区启动新任务")
    run.add_argument("task", help="需要 Agent 完成的任务")
    _add_shared_arguments(run, suppress_defaults=True)
    run.add_argument("--session-id")
    run.add_argument("--task-id")
    run.add_argument("--run-id")
    run.add_argument("--jsonl", action="store_true", help="输出脱敏进度事件和最终 JSON")
    resume = commands.add_parser("resume", help="核验后继续一个已保存会话；不重放未知工具")
    _add_shared_arguments(resume, suppress_defaults=True)
    resume.add_argument("session_id")
    resume.add_argument("task", nargs="?", help="可选续问；省略则进入交互")
    resume.add_argument("--jsonl", action="store_true")
    sessions = commands.add_parser("sessions", help="查看、协调或删除私有持久会话")
    _add_shared_arguments(sessions, suppress_defaults=True)
    actions = sessions.add_subparsers(dest="session_action", required=True)
    actions.add_parser("list", help="列出本状态目录的会话")
    actions.add_parser("prune", help="清理已过期且安全结束的会话，保留未知任务")
    show = actions.add_parser("show", help="默认只展示元数据；正文需显式开启")
    show.add_argument("session_id")
    show.add_argument("--messages", action="store_true", help="在终端展示私有对话正文")
    reconcile = actions.add_parser("reconcile", help="独立核验资源后接受当前工作区，不重放未知调用")
    reconcile.add_argument("session_id")
    reconcile.add_argument("--accept-workspace", action="store_true", required=True)
    delete = actions.add_parser("delete", help="删除已结束会话及私有正文")
    delete.add_argument("session_id")
    return parser


def _add_shared_arguments(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """给交互和单次入口添加相同配置覆盖，避免形成两套启动语义。"""
    missing = argparse.SUPPRESS if suppress_defaults else None
    workspace_default = argparse.SUPPRESS if suppress_defaults else Path.cwd()
    parser.add_argument("--workspace", type=Path, default=workspace_default)
    parser.add_argument(
        "--config",
        type=Path,
        default=missing,
        help="最高优先级 YAML 或 TOML 配置文件",
    )
    parser.add_argument("--provider", choices=("fake", "openai-compatible"), default=missing)
    parser.add_argument("--model", default=missing)
    parser.add_argument("--base-url", default=missing)
    parser.add_argument("--api-key-env", default=missing)
    parser.add_argument("--backend", choices=("local", "docker"), default=missing)
    parser.add_argument("--docker-image", default=missing)
    parser.add_argument(
        "--permission",
        choices=("read-only", "ask-edits", "controlled-edit"),
        default=missing,
    )
    parser.add_argument("--state-directory", type=Path, default=missing)
    parser.add_argument(
        "--worktree",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="隔离模式：从固定 Git commit 创建任务 worktree 并交付补丁",
    )
    parser.add_argument(
        "--direct-workspace",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--base-commit", default=missing, help="任务 worktree 的固定 Git 起点")


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
    git_workspace: dict[str, object] = {}
    if getattr(arguments, "worktree", False) and getattr(arguments, "direct_workspace", False):
        raise ValueError("--worktree and --direct-workspace cannot be used together")
    if getattr(arguments, "worktree", False):
        git_workspace["enabled"] = True
    elif getattr(arguments, "direct_workspace", False):
        # 旧版参数继续接受，便于已有脚本迁移；默认值现在已经是直接工作区。
        git_workspace["enabled"] = False
    if getattr(arguments, "base_commit", None) is not None:
        git_workspace["base_commit"] = arguments.base_commit
    if git_workspace:
        result["git_workspace"] = git_workspace
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
    delivery_failed = result.delivery is not None and result.delivery.state == "failed_retained"
    return 0 if result.report.reason == "completed" and not delivery_failed else 1


def _interactive(arguments: argparse.Namespace, config: ProductConfig) -> int:
    """持续读取终端任务；单次失败只结束当前任务，不关闭整个交互进程。"""
    session_id = getattr(arguments, "session_id", None) or f"session-{uuid.uuid4().hex}"
    print("CodePilot 交互模式。输入任务后回车执行；/help 查看帮助，/exit 退出。")
    print(f"工作区：{arguments.workspace}｜会话：{session_id}")
    print(
        "对话与工具输出将保存在私有会话目录；保留期见配置。"
        "可用 sessions delete 删除、sessions prune 清理过期会话。"
    )
    while True:
        try:
            task = input("codepilot> ").strip()
        except EOFError:
            # 管道关闭或 Ctrl-D 表示调用方已经没有更多任务，应正常退出。
            print()
            return 0
        except KeyboardInterrupt:
            # 在提示符处按 Ctrl-C 只清空当前输入，第二次仍可继续使用界面。
            print("\n输入已取消；使用 /exit 退出。")
            continue
        # 空行只用于整理输入，不应创建 ID、Trace 或模型请求。
        if not task:
            continue
        # 退出和帮助是交互外壳命令，不能作为不可信任务发送给模型。
        if task in {"/exit", "/quit"}:
            return 0
        if task == "/help":
            print(
                "直接输入自然语言任务；/exit 或 /quit 退出。"
                "任务共享持久会话，每次执行有独立 Trace。"
            )
            continue
        task_arguments = argparse.Namespace(
            **{
                **vars(arguments),
                "task": task,
                "session_id": session_id,
                "task_id": None,
                "run_id": None,
                "jsonl": False,
            }
        )
        try:
            exit_code = asyncio.run(_run(task_arguments, config))
            if exit_code != 0:
                # Runtime 的受控失败已经由 renderer 展示；这里说明交互会话仍可继续。
                print("[任务未完成] 可以检查 Trace 后继续输入下一条任务。", file=sys.stderr)
        except KeyboardInterrupt:
            # asyncio.run 已取消当前主任务；交互外壳继续等待下一条用户输入。
            print("\n当前任务已取消，可以继续输入。", file=sys.stderr)
        except (FileNotFoundError, NotADirectoryError, ValidationError, ValueError) as error:
            # 已知配置错误只含字段和环境变量名，应保留正文帮助用户修正启动条件。
            print(f"当前任务配置错误：{error}；可以继续输入。", file=sys.stderr)
        except Exception as error:
            # 未知异常只展示类型，避免 Provider 异常正文携带请求内容或秘密。
            print(f"当前任务启动失败：{type(error).__name__}；可以继续输入。", file=sys.stderr)


def _sessions(arguments: argparse.Namespace, config: ProductConfig) -> int:
    """管理会话元数据与显式正文查看，默认不把私有内容输出到终端。"""
    root = _state_root(config).absolute()
    action = arguments.session_action
    if action == "show":
        result = SessionStore.inspect(root, arguments.session_id, messages=arguments.messages)
        result["outputs"] = [
            json.loads(path.read_bytes())
            for path in (root / arguments.session_id / "outputs").glob("output-*.json")
        ]
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if action == "list":
        for path in sorted(root.glob("*/session.db")):
            snapshot = SessionStore.inspect(root, path.parent.name)
            print(
                json.dumps(
                    {
                        "session_id": snapshot["session_id"],
                        "status": snapshot["status"],
                        "expired": snapshot["expires_at"] is not None
                        and snapshot["expires_at"] < time.time(),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    if action == "prune":
        for path in sorted(root.glob("*/session.db")):
            snapshot = SessionStore.inspect(root, path.parent.name)
            # 活跃或未知会话不竞争执行锁；到期只代表可检查删除，不代表允许丢弃代码。
            if (
                snapshot["status"] != "ready"
                or snapshot["expires_at"] is None
                or snapshot["expires_at"] >= time.time()
            ):
                continue
            with SessionStore(root, path.parent.name, create=False) as session:
                try:
                    session.ensure_deletable()
                except ValueError:
                    print(f"保留含工作树或补丁的会话 {session.session_id}")
                    continue
                shutil.rmtree(session.directory)
                print(f"已删除过期会话 {session.session_id}")
        return 0
    with SessionStore(root, arguments.session_id, create=False) as session:
        if action == "reconcile":
            session.reconcile(arguments.workspace.resolve(strict=True))
            print("已核验并接受当前工作区；未知调用已标记，未重放任何工具。")
        elif action == "delete":
            session.ensure_deletable()
            # 持锁时先删除正文与数据库，锁文件最后由目录删除；不影响其他会话。
            shutil.rmtree(session.directory)
            print("会话及私有正文已删除。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """解析一次配置，再进入交互外壳或执行一个非交互任务。"""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        workspace = arguments.workspace.resolve(strict=True)
        config = load_product_config(
            workspace=workspace,
            explicit_path=arguments.config,
            overrides=_overrides(arguments),
        )
        # 有 run 子命令时执行一次并返回状态；无子命令才进入持续提示符。
        if arguments.command == "sessions":
            return _sessions(arguments, config)
        if arguments.command == "resume":
            # 不存在会话或身份变化必须在提示符和模型请求前明确失败。
            with SessionStore(_state_root(config), arguments.session_id, create=False) as session:
                session.check(workspace, f"{config.backend.kind}:{config.git_workspace.enabled}")
            if arguments.task is not None:
                arguments.task_id = None
                arguments.run_id = None
                return asyncio.run(_run(arguments, config))
            return _interactive(arguments, config)
        if arguments.command == "run":
            return asyncio.run(_run(arguments, config))
        return _interactive(arguments, config)
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
