"""实现受工作区限制的 UTF-8 文本读取工具。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure
from codepilot.tools.file_common import DEFAULT_READ_BYTES, MAX_FILE_BYTES, _read_text_with_hash
from codepilot.tools.workspace import Workspace


class ReadFileArguments(Contract):
    """定义读取路径和允许返回的最大字节数。"""

    mode: Literal["full", "page"] = "full"
    start_line: Annotated[
        int, Field(ge=1, description="从1开始的行号；使用字节 offset 续页时必须为1")
    ] = 1
    line_count: Annotated[int, Field(ge=1, le=2000)] = 200
    offset: (
        Annotated[
            int,
            Field(
                ge=0,
                description="仅用于 next_cursor 的字节偏移，不是行号；必须同时传 expected_sha256",
            ),
        ]
        | None
    ) = None
    expected_sha256: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-fA-F]{64}$", description="续页时原样传回 next_cursor 的文件版本"),
    ] = None
    scan_bytes: Annotated[int, Field(ge=1024, le=64 * 1024**2)] = 16 * 1024**2
    path: Annotated[str, Field(min_length=1)]
    max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = DEFAULT_READ_BYTES
    include_sha256: bool = False


class ReadFileTool(Tool[ReadFileArguments]):
    """读取一个有大小上限的工作区 UTF-8 文本文件。"""

    name = "read_file"
    description = "读取 UTF-8 文件；page 模式返回行窗口、完整版本与 next_cursor，后续页传回游标"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = ReadFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: ReadFileArguments) -> str:
        """解析路径并返回完整且未超过调用上限的文本。"""
        path = self.workspace.resolve(arguments.path, must_exist=True)
        # 独立库调用保留 full 兼容；正式产品默认选择有界分页。
        paging = arguments.mode == "page" or bool(
            arguments.model_fields_set & {"start_line", "line_count", "offset"}
        )
        if paging:
            from codepilot.tools.pages import read_page

            return read_page(
                path,
                start_line=arguments.start_line,
                line_count=arguments.line_count,
                offset=arguments.offset,
                max_bytes=arguments.max_bytes,
                scan_bytes=arguments.scan_bytes,
                expected_sha256=arguments.expected_sha256,
            )
        content, sha256 = _read_text_with_hash(path, arguments.max_bytes)
        if arguments.expected_sha256 is not None and sha256 != arguments.expected_sha256.lower():
            raise ToolFailure("conflict: file version changed")
        # 版本元数据按需返回，保持普通读取结果向后兼容。
        if arguments.include_sha256:
            return f"sha256={sha256}\ncontent:\n{content}"
        return content
