"""提供交互式 ``codepilot`` 与单次 ``codepilot run`` 产品入口。"""

import argparse
import asyncio
import json
import re
import shlex
import shutil
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from pydantic import ValidationError

from codepilot.app import CodePilotApplication, RunIdentity
from codepilot.app.service import _state_root
from codepilot.config import ProductConfig, load_product_config
from codepilot.credentials import load_workspace_dotenv
from codepilot.sessions.context import context_command
from codepilot.sessions.migrations import maintain_database
from codepilot.sessions.store import SessionStore, safe_id
from codepilot.tasks.controls import task_command
from codepilot.ui.renderer import EventRenderer

try:
    import termios as _termios_module
    import tty as _tty_module
except ImportError:  # Windows 使用稳定的整行输入回退，避免依赖 Unix 终端接口。
    _termios: ModuleType | None = None
    _tty: ModuleType | None = None
else:
    _termios = _termios_module
    _tty = _tty_module


def build_parser() -> argparse.ArgumentParser:
    """构造无隐式网络行为的命令行解析器。"""
    parser = argparse.ArgumentParser(prog="codepilot", description="CodePilot 终端 Coding Agent")
    _add_shared_arguments(parser)
    commands = parser.add_subparsers(dest="command")
    fetch = commands.add_parser("fetch", help="零模型 HTTPS GET，需用户配置目的地和逐次审批")
    _add_shared_arguments(fetch, suppress_defaults=True)
    fetch.add_argument("url")
    fetch.add_argument("--secret-ref", default="")
    comparison = commands.add_parser("eval-compare", help="比较同条件完整评测，漂移则拒绝")
    _add_shared_arguments(comparison, suppress_defaults=True)
    comparison.add_argument("first", type=Path)
    comparison.add_argument("second", type=Path)
    for name in ("doctor", "diff", "permissions"):
        operation = commands.add_parser(name, help="无模型的诊断、变更审查或权限查看")
        _add_shared_arguments(operation, suppress_defaults=True)
    for name in ("changes", "undo", "delivery"):
        change = commands.add_parser(
            name,
            help="审查或有条件交付限容副本产物，零模型请求"
            if name == "delivery"
            else "审查或有条件撤销指定 run 的修改，零模型请求",
        )
        _add_shared_arguments(change, suppress_defaults=True)
        change.add_argument("session_id")
        change.add_argument("run_id")
        if name in {"undo", "delivery"}:
            # 默认只预览；交付或撤销都必须由用户显式应用。
            change.add_argument(
                "--apply",
                action="store_true",
                help="明确应用交付；默认仅预览"
                if name == "delivery"
                else "明确应用撤销；默认仅预览",
            )
    recovery = commands.add_parser("recover", help="核验并保存崩溃限容任务的产物，不重放工具")
    _add_shared_arguments(recovery, suppress_defaults=True)
    recovery.add_argument("session_id")
    recovery.add_argument("run_id")
    recovery.add_argument("--apply", action="store_true", help="明确导出并清理；默认仅预览")
    testing = commands.add_parser("test", help="按现有权限执行测试，零模型请求")
    _add_shared_arguments(testing, suppress_defaults=True)
    testing.add_argument("--timeout", type=float, default=120.0)
    testing.add_argument("--network", action="store_true", help="显式申请配置内原生联网，仍需审批")
    testing.add_argument("argv", nargs=argparse.REMAINDER)
    cleanup = commands.add_parser("resources", help="预览/清理会话精确归属资源，不删除代码")
    _add_shared_arguments(cleanup, suppress_defaults=True)
    cleanup.add_argument("action", choices=("prune",))
    cleanup.add_argument("session_id")
    cleanup.add_argument("--apply", action="store_true")
    evaluation = commands.add_parser("eval", help="运行固定版本与隐藏验收的产品评测")
    _add_shared_arguments(evaluation, suppress_defaults=True)
    evaluation.add_argument("manifest", type=Path)
    evaluation.add_argument("--output", required=True, type=Path)
    evaluation.add_argument(
        "--allow-download", action="store_true", help="允许控制器下载清单中的 HTTPS 仓库"
    )
    evaluation.add_argument(
        "--approve-tools", action="store_true", help="明确授权可信清单内有限工具调用"
    )
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
    compact = commands.add_parser("compact", help="核验并压缩已保存会话，不执行工作区工具")
    _add_shared_arguments(compact, suppress_defaults=True)
    compact.add_argument("session_id")
    compact.add_argument("--jsonl", action="store_true")
    tasks = commands.add_parser("tasks", help="查看持久任务或向活跃控制器发送取消/终端输入")
    _add_shared_arguments(tasks, suppress_defaults=True)
    tasks.add_argument("action", choices=("list", "show", "cancel", "input"))
    tasks.add_argument("session_id")
    tasks.add_argument("task_id", nargs="?")
    context = commands.add_parser("context", help="查看上下文、摘要或撤回压缩，不调用模型")
    _add_shared_arguments(context, suppress_defaults=True)
    context.add_argument("session_id")
    context.add_argument("action", nargs="?", choices=("show", "summary", "reset"), default="show")
    sessions = commands.add_parser("sessions", help="查看、协调或删除私有持久会话")
    _add_shared_arguments(sessions, suppress_defaults=True)
    actions = sessions.add_subparsers(dest="session_action", required=True)
    for name in ("backup", "migrate", "facts", "diagnose"):
        operation = actions.add_parser(name, help="备份/迁移或查看控制器事实与恢复诊断")
        operation.add_argument("session_id")
    actions.add_parser("list", help="列出本状态目录的会话")
    actions.add_parser("prune", help="清理已过期且安全结束的会话，保留未知任务")
    restore = actions.add_parser("restore", help="从本会话备份恢复数据库；不回滚代码")
    restore.add_argument("session_id")
    restore.add_argument("backup_name")
    show = actions.add_parser("show", help="默认只展示元数据；正文需显式开启")
    show.add_argument("session_id")
    show.add_argument("--messages", action="store_true", help="在终端展示私有对话正文")
    reconcile = actions.add_parser("reconcile", help="独立核验资源后接受当前工作区，不重放未知调用")
    reconcile.add_argument("session_id")
    reconcile.add_argument("--accept-workspace", action="store_true", required=True)
    reconcile.add_argument(
        "--accept-network-unknown",
        action="store_true",
        help="明确接受远端结果仍未知，不重放旧请求；仅接受本地工作区不会清除此阻断",
    )
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
    parser.add_argument("--protocol", choices=("chat-completions", "responses"), default=missing)
    parser.add_argument("--model", default=missing)
    parser.add_argument("--base-url", default=missing)
    parser.add_argument("--api-key-env", default=missing)
    parser.add_argument("--backend", choices=("local", "docker"), default=missing)
    parser.add_argument("--docker-image", default=missing)
    parser.add_argument(
        "--bounded-workspace-bytes",
        type=int,
        default=missing,
        help="显式 Rootless Docker 限容副本，正常结束后按源版本交付；默认仍直写",
    )
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
            "protocol": getattr(arguments, "protocol", None),
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
    if getattr(arguments, "bounded_workspace_bytes", None) is not None:
        backend.update(
            workspace_mode="bounded-copy",
            limits={"workspace_bytes": arguments.bounded_workspace_bytes},
        )
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
        compact_only=getattr(arguments, "compact_only", False),
    )
    renderer.finished(result)
    delivery_failed = result.delivery is not None and result.delivery.state == "failed_retained"
    delivery_failed = delivery_failed or (
        result.bounded_delivery is not None
        and result.bounded_delivery["state"] not in {"applied", "empty"}
    )
    return (
        0
        if result.report.reason == "completed"
        and not delivery_failed
        and not result.needs_reconciliation
        else 1
    )


def _interactive(arguments: argparse.Namespace, config: ProductConfig) -> int:
    """持续读取终端任务；单次失败只结束当前任务，不关闭整个交互进程。"""
    session_id = getattr(arguments, "session_id", None) or f"session-{uuid.uuid4().hex}"
    renderer = EventRenderer(sys.stdout)
    renderer.welcome(arguments.workspace, config.provider.model, config.backend.kind, session_id)
    print(
        "对话与工具输出将保存在私有会话目录；保留期见配置。"
        "可用 sessions delete 删除、sessions prune 清理过期会话。"
    )
    while True:
        try:
            task = _read_interactive_task("❯ ").strip()
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
        compact_only = task == "/compact"
        try:
            if not compact_only and _handle_slash_command(task, arguments, config, session_id):
                continue
        except KeyboardInterrupt:
            # 斜杠测试与模型任务一样可取消；清理后把输入权交回提示符。
            print("\n命令已取消；可继续输入任务。")
            continue
        if _looks_like_cli_command(task):
            # 交互提示符内的 CLI 命令不能交给模型猜测，否则会触发无关工具探索或长流等待。
            print("检测到终端命令；请先输入 /exit 退出交互模式，再在 Shell 中执行：" + task)
            continue
        task_arguments = argparse.Namespace(
            **{
                **vars(arguments),
                "task": task,
                "compact_only": compact_only,
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


def _read_interactive_task(prompt: str) -> str:
    """读取一行任务；真实 TTY 中提供斜杠命令提示，管道测试继续使用 input。"""
    if _termios is None or _tty is None or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return input(prompt)
    original = _termios.tcgetattr(sys.stdin)
    buffer: list[str] = []
    menu_shown = False
    try:
        _tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            char = sys.stdin.read(1)
            if char in {"\n", "\r"}:
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(buffer)
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x04":
                raise EOFError
            if char in {"\x7f", "\b"}:
                if buffer:
                    buffer.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char == "\x1b":
                # 方向键等控制序列不进入任务正文；菜单选择留给后续 TUI 增量。
                continue
            if not char.isprintable():
                continue
            buffer.append(char)
            sys.stdout.write(char)
            if "".join(buffer) == "/" and not menu_shown:
                # 只在用户明确开始斜杠命令时显示，普通自然语言输入不被打扰。
                sys.stdout.write(
                    "\n  /help /status /model /sessions /tasks /context /compact "
                    "/doctor /diff /changes /undo /delivery /recover /permissions "
                    "/fetch /test /exit\n❯ /"
                )
                menu_shown = True
            sys.stdout.flush()
    finally:
        # 无论 EOF、Ctrl-C 还是异常，都必须恢复终端回显和行模式。
        _termios.tcsetattr(sys.stdin, _termios.TCSADRAIN, original)


def _handle_slash_command(
    task: str, arguments: argparse.Namespace, config: ProductConfig, session_id: str
) -> bool:
    """处理交互外壳命令并返回是否不应将输入发送给模型。"""
    if not task.startswith("/"):
        return False
    command = task.split(maxsplit=1)[0]
    if command == "/help":
        print(
            "/help 帮助  /status 当前运行状态  /model 当前模型  "
            "/sessions 会话列表  /tasks 后台任务  /context 上下文  /compact 压缩历史  "
            "/doctor 诊断 /diff 变更 /changes RUN 归属 /undo RUN [--apply] 撤销 "
            "/delivery RUN [--apply] 受限副本交付 "
            "/permissions 权限 /test [--network] 命令 /exit 退出"
            " /recover RUN [--apply] 恢复限容产物"
            " /fetch URL [--secret-ref NAME] 受控 HTTPS"
        )
        return True
    if command == "/fetch":
        from codepilot.app.operations import fetch

        parts = shlex.split(task)
        if len(parts) not in {2, 4} or (len(parts) == 4 and parts[2] != "--secret-ref"):
            raise ValueError("用法：/fetch URL [--secret-ref NAME]")
        data = asyncio.run(
            fetch(config, arguments.workspace, parts[1], parts[3] if len(parts) == 4 else "")
        )
        print(json.dumps(data, ensure_ascii=False))
        return True
    if command == "/recover":
        from codepilot.app.operations import recover

        parts = shlex.split(task)
        if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] != "--apply"):
            raise ValueError("用法：/recover RUN_ID [--apply]")
        print(
            json.dumps(
                asyncio.run(
                    recover(
                        config, arguments.workspace, session_id, parts[1], apply=len(parts) == 3
                    )
                ),
                ensure_ascii=False,
            )
        )
        return True
    if command in {"/changes", "/undo", "/delivery"}:
        from codepilot.app.operations import changes

        try:
            parts = shlex.split(task)
            if len(parts) not in {2, 3} or (
                len(parts) == 3 and (command not in {"/undo", "/delivery"} or parts[2] != "--apply")
            ):
                raise ValueError("用法：/changes RUN_ID 或 /undo RUN_ID [--apply]")
            data = changes(
                config,
                arguments.workspace,
                session_id,
                parts[1],
                apply=len(parts) == 3,
                **({"deliver": True} if command == "/delivery" else {}),
            )
            print(json.dumps(data, ensure_ascii=False))
        except (ValueError, OSError) as error:
            # 审查错误只结束本条外壳命令，不把恢复操作交给模型猜测。
            print(f"变更操作未完成：{error}", file=sys.stderr)
        return True
    if command in {"/doctor", "/diff", "/permissions", "/test"}:
        from codepilot.app import operations

        if command == "/permissions":
            data = operations.permissions(config)
        elif command == "/doctor":
            data = asyncio.run(operations.doctor(config, arguments.workspace))
        elif command == "/diff":
            data = asyncio.run(operations.diff(arguments.workspace))
        else:
            argv = shlex.split(task.partition(" ")[2])
            network = bool(argv and argv[0] == "--network")
            data = asyncio.run(
                operations.test_command(
                    config,
                    arguments.workspace,
                    argv[1:] if network else argv,
                    120.0,
                    network=network,
                )
            )
        print(json.dumps(data, ensure_ascii=False))
        return True
    if command == "/context":
        action = task.split(maxsplit=1)[1].strip() if " " in task else "show"
        if action not in {"show", "summary", "reset"}:
            print("用法：/context [show|summary|reset]", file=sys.stderr)
            return True
        try:
            print(
                json.dumps(
                    context_command(_state_root(config), session_id, action), ensure_ascii=False
                )
            )
        except ValueError as error:
            print(f"上下文操作未完成：{error}", file=sys.stderr)
        return True
    if command == "/status":
        print(
            f"会话 {session_id}｜目录 {arguments.workspace}｜"
            f"模型 {config.provider.model}｜后端 {config.backend.kind}"
        )
        return True
    if command == "/model":
        print(f"当前模型：{config.provider.model}（{config.provider.kind}）")
        return True
    if command == "/tasks":
        try:
            print(
                json.dumps(
                    task_command(_state_root(config), session_id, "list"), ensure_ascii=False
                )
            )
        except ValueError as error:
            print(f"任务查询未完成：{error}", file=sys.stderr)
        return True
    if command == "/sessions":
        _sessions(argparse.Namespace(**{**vars(arguments), "session_action": "list"}), config)
        return True
    if command not in {"/exit", "/quit"}:
        print(f"未知命令：{command}；输入 /help 查看可用命令。", file=sys.stderr)
    return True


def _looks_like_cli_command(task: str) -> bool:
    """识别用户误输入到交互提示符中的 CodePilot 子命令。"""
    return bool(
        re.match(
            r"^codepilot(?:\s+(?:run|resume|sessions|compact|context|tasks|doctor|diff|permissions|fetch|test|resources|eval|eval-compare)\b|\s*$)",
            task,
        )
    )


def _sessions(arguments: argparse.Namespace, config: ProductConfig) -> int:
    """管理会话元数据与显式正文查看，默认不把私有内容输出到终端。"""
    root = _state_root(config).absolute()
    action = arguments.session_action
    if action in {"backup", "migrate", "restore"}:
        result = maintain_database(
            root / safe_id(arguments.session_id),
            migrate=action == "migrate",
            restore_name=getattr(arguments, "backup_name", None),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if action in {"facts", "diagnose"}:
        with SessionStore(root, arguments.session_id, create=False) as session:
            result = (
                session.facts_view(arguments.workspace.resolve(strict=True))
                if action == "facts"
                else session.diagnose()
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
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
            session.reconcile(
                arguments.workspace.resolve(strict=True),
                accept_network_unknown=arguments.accept_network_unknown,
            )
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
        # 在读取配置和解析密钥前自动载入当前项目 .env；用户环境变量优先。
        load_workspace_dotenv(workspace)
        config = load_product_config(
            workspace=workspace,
            explicit_path=arguments.config,
            overrides=_overrides(arguments),
        )
        if arguments.command == "fetch":
            from codepilot.app.operations import fetch

            data = asyncio.run(fetch(config, workspace, arguments.url, arguments.secret_ref))
            print(json.dumps(data, ensure_ascii=False))
            return (
                0
                if data["request_succeeded"]
                and data["backend_closed"]
                and not data["needs_reconciliation"]
                else 1
            )
        if arguments.command == "recover":
            from codepilot.app.operations import recover

            data = asyncio.run(
                recover(
                    config, workspace, arguments.session_id, arguments.run_id, apply=arguments.apply
                )
            )
            print(json.dumps(data, ensure_ascii=False))
            return 0
        if arguments.command in {"changes", "undo", "delivery"}:
            from codepilot.app.operations import changes

            data = changes(
                config,
                workspace,
                arguments.session_id,
                arguments.run_id,
                apply=arguments.command in {"undo", "delivery"} and arguments.apply,
                deliver=arguments.command == "delivery",
            )
            print(json.dumps(data, ensure_ascii=False))
            return 1 if data["conflicts"] else 0
        if arguments.command == "eval-compare":
            from codepilot.evals.product import compare_reports

            print(
                json.dumps(compare_reports(arguments.first, arguments.second), ensure_ascii=False)
            )
            return 0
        if arguments.command in {"doctor", "diff", "permissions", "test", "resources", "eval"}:
            from codepilot.app import operations

            if arguments.command == "doctor":
                data = asyncio.run(operations.doctor(config, workspace))
                print(json.dumps(data, ensure_ascii=False))
                return 0 if data["ready"] else 1
            if arguments.command == "diff":
                data = asyncio.run(operations.diff(workspace))
            elif arguments.command == "permissions":
                data = operations.permissions(config)
            elif arguments.command == "resources":
                data = asyncio.run(
                    operations.prune(config, arguments.session_id, apply=arguments.apply)
                )
            elif arguments.command == "test":
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "command_started",
                            "backend": config.backend.kind,
                        }
                    ),
                    flush=True,
                )
                data = asyncio.run(
                    operations.test_command(
                        config,
                        workspace,
                        arguments.argv,
                        arguments.timeout,
                        network=arguments.network,
                    )
                )
                print(json.dumps(data, ensure_ascii=False))
                return (
                    0
                    if (
                        data["command_succeeded"]
                        and data["backend_closed"]
                        and not data["needs_reconciliation"]
                    )
                    else 1
                )
            else:
                from codepilot.evals.product import ProductHarness, Suite

                raw = json.loads(arguments.manifest.read_bytes())
                for task in raw["tasks"]:
                    for key in ("repository", "validation_material"):
                        if not task[key].startswith("https://"):
                            task[key] = str((arguments.manifest.parent / task[key]).resolve())
                suite = Suite.model_validate(raw)

                def progress(event: dict[str, object]) -> None:
                    """持续输出有版本的试验进度，正文和凭据不进入批量摘要。"""
                    print(json.dumps(event, ensure_ascii=False), flush=True)

                data = asyncio.run(
                    ProductHarness(
                        config,
                        arguments.output,
                        approve_tools=arguments.approve_tools,
                        allow_download=arguments.allow_download,
                        on_progress=progress,
                    ).run(suite)
                )
                print(
                    json.dumps(
                        {key: value for key, value in data.items() if key != "results"},
                        ensure_ascii=False,
                    )
                )
                return 0 if data["task_gate_passed"] else 1
            print(json.dumps(data, ensure_ascii=False))
            return 0
        # 有 run 子命令时执行一次并返回状态；无子命令才进入持续提示符。
        if arguments.command == "tasks":
            control_input = ""
            if arguments.action == "input":
                # 用户输入不进入模型或 shell 参数；控制信箱 0600，消费后立即删除。
                import getpass

                control_input = (
                    getpass.getpass("终端输入（不回显）：") + "\n"
                    if sys.stdin.isatty()
                    else sys.stdin.read(4097)
                )
            print(
                json.dumps(
                    task_command(
                        _state_root(config),
                        arguments.session_id,
                        arguments.action,
                        arguments.task_id,
                        control_input,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        if arguments.command == "context":
            print(
                json.dumps(
                    context_command(_state_root(config), arguments.session_id, arguments.action),
                    ensure_ascii=False,
                )
            )
            return 0
        if arguments.command == "sessions":
            return _sessions(arguments, config)
        if arguments.command in {"resume", "compact"}:
            # 不存在会话或身份变化必须在提示符和模型请求前明确失败。
            with SessionStore(_state_root(config), arguments.session_id, create=False) as session:
                session.check(workspace, config.execution_mode)
            if arguments.command == "compact":
                arguments.task = "/compact"
                arguments.compact_only = True
                arguments.task_id = None
                arguments.run_id = None
                return asyncio.run(_run(arguments, config))
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
