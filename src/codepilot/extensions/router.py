"""把技能、后台执行、MCP 与子任务作为普通工具接入现有权限及调度链。"""

import asyncio
import hashlib
import json
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from codepilot.contracts import ToolCall, ToolResult
from codepilot.contracts.models import Contract
from codepilot.extensions.models import ExtensionConfig
from codepilot.extensions.skills import SkillCatalog, scoped_rules
from codepilot.sessions.outputs import CURRENT_CALL
from codepilot.tasks.manager import TaskManager
from codepilot.tasks.terminal import TerminalProcess
from codepilot.tools.base import Tool, ToolFailure, ToolOutput, ToolSpec
from codepilot.tools.registry import ToolRegistry
from codepilot.workspace.base import ExecutionBackend


class SkillArguments(Contract):
    """仅通过发现的名称读取技能，不允许任意宿主路径。"""

    name: str


class RulesArguments(Contract):
    """读取目标相对文件的作用域规则。"""

    path: str


class StartArguments(Contract):
    """后台进程与普通 Shell 一样需要 process 审批，时限由父配置决定。"""

    command: Annotated[str, Field(min_length=1, max_length=8192)]
    interactive: bool = False


class TaskArguments(Contract):
    """只操作当前运行生成的任务身份，输出游标保持有界。"""

    task_id: str
    cursor: Annotated[int, Field(ge=0)] = 0


class InputArguments(Contract):
    """向已拥有的终端发送有限输入，窗口与 EOF 不需要伪造 Shell 命令。"""

    task_id: str
    data: Annotated[str, Field(max_length=4096)] = ""
    eof: bool = False
    rows: Annotated[int, Field(ge=1, le=200)] = 24
    columns: Annotated[int, Field(ge=1, le=500)] = 80


class DelegateArguments(Contract):
    """只读探索和审查没有 Shell、写入或递归委派能力。"""

    task: Annotated[str, Field(min_length=1, max_length=4000)]
    role: Literal["explore", "review"] = "explore"


class McpArguments(Contract):
    """服务器名来自用户配置，工具名在各自命名空间内解析。"""

    server: str
    tool: str = ""
    arguments: dict[str, Any] = {}


class HookArguments(Contract):
    """Hook 只接收控制器绑定的生命周期元数据，不能修改原工具参数。"""

    name: str
    call_id: str
    tool: str
    event: str


class FunctionalTool(Tool[BaseModel]):
    """复用已有 ToolRegistry 校验/超时，只把不同扩展绑定到实际处理函数。"""

    def __init__(
        self,
        name: str,
        model: type[BaseModel],
        effect: Literal["read", "write", "process"],
        handler: Callable[[Any], Awaitable[str | ToolOutput]],
        *,
        background: bool = False,
        control: bool = False,
    ) -> None:
        """设置一份实际可执行声明，不为每个扩展复制注册与异常逻辑。"""
        self.name, self.arguments_model = name, model
        self.effect = effect  # 类型由下方装配时的固定字面量限制。
        self.resources = ("workspace",)
        self.description = {
            "load_skill": "按发现名称加载技能资料，不授予权限",
            "load_rules": "读取相对文件适用的目录规则",
            "task_start": "启动后台命令；运行结束前必须查看结果或取消；前台退出会终止",
            "task_status": "按任务 ID 查看状态与有界输出；使用 read_output 读取完整输出引用",
            "task_cancel": "取消当前运行拥有的后台任务并等待清理",
            "task_input": "向当前运行拥有的交互终端发送输入、EOF 或窗口尺寸",
            "delegate": "派生只读 explore/review 子任务，共享预算；用 task_status 查看结果",
            "mcp_list": "列出用户配置的本地 MCP 服务器工具及参数 Schema，需 process 授权",
            "mcp_call": "在独立服务器命名空间调用 MCP 工具，未知副作用不重试",
            "run_hook": "内部受控生命周期 Hook，不对模型公开",
        }[name]
        self.default_timeout = 120.0
        self.handler, self.background, self.control = handler, background, control
        self.provenance: Callable[[], str] = lambda: ""

    @property
    def spec(self) -> ToolSpec:
        """调度信息只由可信内部工具声明，MCP 注释不能冒充后台/控制能力。"""
        return super().spec.model_copy(
            update={
                "background": self.background,
                "control": self.control,
                "provenance": self.provenance(),
            }
        )

    async def run(self, arguments: BaseModel) -> str | ToolOutput:
        """参数已经由唯一注册表校验，继续执行具体扩展处理器。"""
        return await self.handler(arguments)


class ExtensionRouter:
    """路由已授权工具；所有 process 操作使用同一受控后端或其任务实例。"""

    def __init__(
        self,
        backend: ExecutionBackend,
        config: ExtensionConfig,
        manager: TaskManager,
        backend_factory: Callable[[str], ExecutionBackend],
        extra_tools: tuple[Tool[Any], ...] = (),
    ) -> None:
        """发现资料并装配真实工具，可执行扩展关闭时不暴露执行入口。"""
        self.registry, self.config, self.manager = backend, config, manager
        self.root = Path(backend.root)
        self.backend_factory = backend_factory
        self.skills = SkillCatalog(self.root) if config.skills else None
        self.delegate_handler: Callable[[DelegateArguments], Awaitable[str]] | None = None
        self.terminals: dict[str, TerminalProcess] = {}
        self.mcp_catalogs: dict[str, dict[str, Any]] = {}
        tools: list[Tool[Any]] = [FunctionalTool("load_rules", RulesArguments, "read", self.rules)]
        if self.skills is not None:
            tools.append(FunctionalTool("load_skill", SkillArguments, "read", self.skill))
        if config.enabled:
            tools.extend(
                [
                    FunctionalTool(
                        "task_start", StartArguments, "process", self.start, background=True
                    ),
                    FunctionalTool("task_status", TaskArguments, "read", self.status, control=True),
                    FunctionalTool("task_cancel", TaskArguments, "read", self.cancel, control=True),
                    FunctionalTool(
                        "task_input", InputArguments, "process", self.input, control=True
                    ),
                    FunctionalTool("delegate", DelegateArguments, "read", self.delegate),
                ]
            )
            if config.mcp:
                tools.extend(
                    [
                        FunctionalTool("mcp_list", McpArguments, "process", self.mcp_list),
                        FunctionalTool("mcp_call", McpArguments, "process", self.mcp_call),
                    ]
                )
            if config.hooks:
                tools.append(FunctionalTool("run_hook", HookArguments, "process", self.hook))
        for tool in tools:
            if isinstance(tool, FunctionalTool) and tool.effect == "process":
                tool.provenance = self._provenance
        self.extras = ToolRegistry((*tools, *extra_tools))

    def _provenance(self) -> str:
        """配置与仓库脚本版本进入审批指纹，审批中替换代码必须重新授权。"""
        digest = hashlib.sha256(self.config.model_dump_json().encode())
        commands = [item.argv for item in self.config.hooks] + [
            item.argv for item in self.config.mcp
        ]
        for argv in commands:
            for value in argv:
                path = Path(value)
                # 工作区绝对路径同样绑定审批版本，容器路径先映射回实际工作区。
                if path.is_absolute():
                    base = Path("/workspace") if self.registry.is_container else self.root
                    try:
                        value = path.relative_to(base).as_posix()
                        path = Path(value)
                    except ValueError:
                        # 外部用户程序依赖宿主信任；不读取仓库之外的任意脚本或秘密。
                        continue
                if len(value) < 256 and not path.is_absolute() and path.suffix in {".py", ".sh"}:
                    digest.update(scoped_rules(self.root, value).encode())
                    from codepilot.extensions.skills import safe_text

                    digest.update(safe_text(self.root, value, 65536).encode())
        return digest.hexdigest()

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """主工具仍来自后端，扩展不能覆盖内置同名工具。"""
        return (*self.registry.specs, *self.extras.specs)

    async def execute(self, call: ToolCall) -> ToolResult:
        """动态工具先走注册表；普通文件工具返回有来源的作用域规则。"""
        if call.name in {spec.name for spec in self.extras.specs}:
            return await self.extras.execute(call)
        result = await self.registry.execute(call)
        if call.name == "read_file" and isinstance(call.arguments.get("path"), str):
            try:
                rules = scoped_rules(self.root, str(call.arguments["path"]))
                if rules:
                    return result.model_copy(update={"content": result.content + "\n" + rules})
            except (OSError, ValueError, ToolFailure):
                return ToolResult(
                    call_id=call.call_id, content="scoped rules unavailable", is_error=True
                )
        return result

    async def skill(self, arguments: SkillArguments) -> str:
        """正文加载前重新检查发现时哈希。"""
        assert self.skills is not None
        return self.skills.load(arguments.name)

    async def rules(self, arguments: RulesArguments) -> str:
        """按相对文件路径获取作用域规则，来源不具有执行授权。"""
        return scoped_rules(self.root, arguments.path) or "No scoped rules"

    async def start(self, arguments: StartArguments) -> str:
        """任务 ID 返回后租约继续存在，子后端由任务 finally 负责清理。"""
        call_id = CURRENT_CALL.get()
        terminal = TerminalProcess() if arguments.interactive else None

        async def operation() -> str:
            """实际副作用只在审批和调度后发生，单个任务取消不杀主后端。"""
            backend = self.backend_factory(call_id)
            try:
                await backend.start()
                self._resources(backend)
                async with asyncio.timeout(self.config.task_timeout_seconds):
                    if terminal is not None:
                        command = arguments.command
                        # Docker 分配第二层 PTY，宿主关闭回显不能替代容器内终端设置。
                        if backend.is_container:
                            command = "stty -echo && exec /bin/sh -c " + shlex.quote(command)
                        argv = self._stdio(backend, ("/bin/sh", "-c", command), tty=True)
                        output = await terminal.run(
                            argv, self.root, call_id, lambda text: self._chunk(call_id, text)
                        )
                        if not output.startswith("exit_code=0\n"):
                            raise ToolFailure("interactive command failed")
                        return output
                    result = await backend.run_argv(
                        ("/bin/sh", "-c", arguments.command),
                        timeout_seconds=self.config.task_timeout_seconds,
                    )
                    text = json.dumps(result.model_dump(), ensure_ascii=False)
                    if result.exit_code != 0 or result.timed_out:
                        identity = next(
                            key
                            for key, row in self.manager.records.items()
                            if row["parent_call"] == call_id
                        )
                        self.manager.save(identity, output=text[:16000])
                        raise ToolFailure("background command failed")
                    return text
            finally:
                await self._close_backend(backend)

        identity = self.manager.start(call_id, "terminal" if terminal else "process", operation)
        if terminal is not None:
            self.terminals[identity] = terminal
        return json.dumps({"task_id": identity, "state": "queued", "owner": "current_run"})

    async def _close_backend(self, backend: ExecutionBackend) -> None:
        """清理失败阻断父级正常结算，资源清单不能被主后端的成功关闭抹除。"""
        try:
            await backend.close()
        except BaseException:
            self.manager.cleanup_failed = True
            raise

    def _resources(self, backend: ExecutionBackend) -> None:
        """登记真实容器身份，控制器崩溃后仍可拒绝不确定恢复。"""
        for event in backend.events:
            if event.container_id:
                self.manager.session.resource("container", {"id": event.container_id})
                break

    def _stdio(
        self, backend: ExecutionBackend, argv: tuple[str, ...], *, tty: bool = False
    ) -> tuple[str, ...]:
        """容器模式只执行该任务容器，不回退到宿主 Shell 或挂载凭据。"""
        if not backend.is_container:
            return argv
        identity = next(
            (event.container_id for event in reversed(backend.events) if event.container_id), ""
        )
        if not identity:
            raise ToolFailure("container not ready")
        return ("docker", "exec", "-it" if tty else "-i", identity, *argv)

    def _chunk(self, call_id: str, text: str) -> None:
        """保存有限交互日志，原始输出仍由共享 OutputStore 按配额保留。"""
        identity = next(
            key for key, row in self.manager.records.items() if row["parent_call"] == call_id
        )
        record = self.manager.records[identity]
        self.manager.save(identity, output=(record["output"] + text)[:16000])

    async def status(self, arguments: TaskArguments) -> str:
        """让后台任务至少有一次调度机会，随后返回有界状态和游标。"""
        await asyncio.sleep(0)
        return json.dumps(
            self.manager.status(arguments.task_id, arguments.cursor), ensure_ascii=False
        )

    async def cancel(self, arguments: TaskArguments) -> str:
        """取消精确任务并等待其后端退出。"""
        await self.manager.cancel(arguments.task_id)
        return json.dumps(self.manager.status(arguments.task_id))

    async def input(self, arguments: InputArguments) -> str:
        """输入不落入任务输出日志；程序自身输出仍属于私有原文。"""
        if arguments.task_id not in self.terminals:
            raise ToolFailure("unknown interactive task")
        self.terminals[arguments.task_id].input(
            arguments.data, eof=arguments.eof, rows=arguments.rows, columns=arguments.columns
        )
        return "input delivered"

    async def delegate(self, arguments: DelegateArguments) -> str:
        """只读子任务以父运行拥有的任务句柄返回，不能自行扩大能力。"""
        if self.delegate_handler is None:
            raise ToolFailure("child provider unavailable")
        handler = self.delegate_handler
        identity = self.manager.start(
            CURRENT_CALL.get(), arguments.role, lambda: handler(arguments)
        )
        return json.dumps({"task_id": identity, "state": "queued", "kind": arguments.role})

    async def mcp_list(self, arguments: McpArguments) -> str | ToolOutput:
        """目录发现也启动进程，因此不能把它当作默认安全的只读调用。"""
        return await self._mcp(arguments, listing=True)

    async def mcp_call(self, arguments: McpArguments) -> str | ToolOutput:
        """Schema 重新核验后才调用，未知副作用错误不自动重发。"""
        return await self._mcp(arguments, listing=False)

    async def _mcp(self, arguments: McpArguments, *, listing: bool) -> ToolOutput:
        """每个调用独占一个受管服务器实例，关闭时一并清理服务端后代。"""
        from codepilot.extensions.mcp import request_mcp

        config = next((item for item in self.config.mcp if item.name == arguments.server), None)
        if config is None:
            raise ToolFailure("unknown configured MCP server")
        backend = self.backend_factory(f"mcp-{uuid4().hex}")
        try:
            async with asyncio.timeout(config.timeout_seconds):
                await backend.start()
                self._resources(backend)
                catalog, text, error = await request_mcp(
                    self._stdio(backend, tuple(config.argv)),
                    str(self.root),
                    self.mcp_catalogs.get(config.name),
                    None if listing else arguments.tool,
                    arguments.arguments,
                )
                if listing:
                    self.mcp_catalogs[config.name] = catalog
                return ToolOutput(content=text, is_error=error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ToolFailure(
                f"MCP {type(error).__name__}; outcome unknown; do not replay"
            ) from None
        finally:
            await self._close_backend(backend)

    async def hook(self, arguments: HookArguments) -> ToolOutput:
        """可信 Hook 命令以元数据 JSON 参数启动，固定参数不接受返回改写。"""
        config = next((item for item in self.config.hooks if item.name == arguments.name), None)
        if config is None or config.event != arguments.event:
            raise ToolFailure("unknown hook")
        result = await self.registry.run_argv(
            (*config.argv, arguments.model_dump_json()), timeout_seconds=config.timeout_seconds
        )
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
            raise ToolFailure("hook process failed")
        try:
            parsed = json.loads(result.stdout)
        except ValueError as error:
            raise ToolFailure("invalid hook JSON") from error
        if (
            not isinstance(parsed, dict)
            or set(parsed) - {"decision", "message"}
            or parsed.get("decision") not in {"continue", "block"}
        ):
            raise ToolFailure("invalid hook result")
        return ToolOutput(
            content=str(parsed.get("message", ""))[:1000], is_error=parsed["decision"] == "block"
        )
