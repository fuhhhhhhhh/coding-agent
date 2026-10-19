"""使用原生 rg 选择忽略规则下的文件，再有界检索文本与上下文并报告覆盖。"""

import fnmatch
import io
import json
from collections import deque
from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure
from codepilot.tools.discovery_common import DEFAULT_EXCLUDES, _validate_pattern
from codepilot.tools.file_common import _read_text
from codepilot.tools.process import collect, spawn, workspace_environment
from codepilot.tools.workspace import Workspace

MAX_SEARCH_OUTPUT_BYTES = 65_536


class SearchArguments(Contract):
    """定义字面检索、忽略选择、上下文以及文件/扫描/返回硬上限。"""

    query: Annotated[str, Field(min_length=1, max_length=4096)]
    pattern: Annotated[str, Field(min_length=1)] = "**/*"
    case_sensitive: bool = True
    include_hidden: bool = False
    include_ignored: bool = False
    context_lines: Annotated[int, Field(ge=0, le=10)] = 0
    max_results: Annotated[int, Field(ge=1, le=1000)] = 200
    max_file_bytes: Annotated[int, Field(ge=1, le=8 * 1024**2)] = 1024**2
    max_scan_bytes: Annotated[int, Field(ge=1, le=128 * 1024**2)] = 32 * 1024**2


class SearchTool(Tool[SearchArguments]):
    """原生忽略规则与有界 Python 字面检索组合，不启动模型可控 Shell。"""

    name = "search"
    description = "搜索字面文本，支持文件过滤、忽略/隐藏选择和上下文；返回扫描覆盖及跳过原因"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = SearchArguments

    def __init__(self, workspace: Workspace) -> None:
        """绑定统一工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: SearchArguments) -> str:
        """固定 argv 调用 rg 枚举，再在明确上限内检索安全普通文件。"""
        _validate_pattern(arguments.pattern)
        if "\n" in arguments.query or "\r" in arguments.query:
            raise ToolFailure("invalid_query: multiline queries are not supported")
        argv = ["rg", "--files", "--null", "--no-config", "--no-ignore-parent", "--no-require-git"]
        if arguments.include_hidden:
            argv.append("--hidden")
        if arguments.include_ignored:
            argv.append("--no-ignore")
        for name in sorted(DEFAULT_EXCLUDES):
            argv.extend(("--glob", f"!**/{name}/**"))
        argv.append(".")
        try:
            process = await spawn(
                tuple(argv), cwd=str(self.workspace.root), env=workspace_environment()
            )
            stdout, cut, _, err_cut, _ = await collect(process, 2 * 1024**2, "search-files")
        except FileNotFoundError as error:
            # 不存在 rg 时不能退回另一套忽略语义并伪称结果完整。
            raise ToolFailure("search_backend_unavailable: install ripgrep") from error
        if process.returncode not in {0, 1}:
            raise ToolFailure("search_enumeration_failed: rg could not enumerate files")
        # 截断尾部可能是不完整文件名，只接受完整 NUL 记录。
        fields = stdout.split(b"\0")[:-1]
        names = sorted(
            {item.decode("utf-8", errors="surrogateescape").removeprefix("./") for item in fields}
        )
        names_cut = len(names) > 20000
        names = names[:20000]
        return self._scan(names, arguments, cut or err_cut or names_cut)

    def _scan(self, names: list[str], arguments: SearchArguments, enumeration_cut: bool) -> str:
        """统计扫描、过滤、跳过、截断，绝不把未扫描范围算成没有结果。"""
        needle = arguments.query if arguments.case_sensitive else arguments.query.casefold()
        results: list[str] = []
        counts = {
            "enumerated": len(names),
            "filtered": 0,
            "scanned": 0,
            "bytes_scanned": 0,
            "matched_lines": 0,
            "skipped": 0,
        }
        reasons: dict[str, int] = {}
        truncated = enumeration_cut
        output_bytes = 0
        shortened_lines = 0
        stop = False
        for name in names:
            # 模型的文件模式仅过滤 rg 的安全候选，不覆盖 .gitignore 的决定。
            patterns = (
                (arguments.pattern, arguments.pattern[3:])
                if arguments.pattern.startswith("**/")
                else (arguments.pattern,)
            )
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
                counts["filtered"] += 1
                continue
            try:
                path = self.workspace.resolve(name, must_exist=True)
                size = path.stat().st_size
                if size > arguments.max_file_bytes:
                    raise ToolFailure("file_too_large")
                if counts["bytes_scanned"] + size > arguments.max_scan_bytes:
                    truncated = True
                    reasons["scan_budget"] = 1
                    break
                content = _read_text(path, arguments.max_file_bytes)
            except (ToolFailure, OSError) as error:
                reason = (
                    str(error).split(":", 1)[0] if isinstance(error, ToolFailure) else "io_error"
                )
                counts["skipped"] += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            counts["scanned"] += 1
            counts["bytes_scanned"] += size
            previous: deque[tuple[int, str]] = deque(maxlen=arguments.context_lines)
            after_until, emitted_until = 0, 0
            # StringIO 不为百万短行构造巨大的 splitlines 列表。
            for number, line in enumerate(io.StringIO(content), 1):
                matched = needle in (line if arguments.case_sensitive else line.casefold())
                candidates: list[tuple[int, str, bool]] = []
                if matched:
                    if counts["matched_lines"] >= arguments.max_results:
                        truncated = True
                        stop = True
                        break
                    counts["matched_lines"] += 1
                    candidates.extend((n, text, False) for n, text in previous if n > emitted_until)
                    after_until = number + arguments.context_lines
                if matched or number <= after_until:
                    candidates.append((number, line.rstrip("\r\n"), matched))
                for n, text, hit in candidates:
                    # 长行匹配可能位于尾部；输出明确标注片段，避免看似完整却丢掉命中。
                    if len(text) > 500:
                        position = (text if arguments.case_sensitive else text.casefold()).find(
                            needle
                        )
                        start = max(0, position - 100) if hit else 0
                        text = (
                            f"[line excerpt at char {start}] "
                            + text[start : start + 500]
                            + " [line truncated]"
                        )
                        shortened_lines += 1
                    entry = f"{name}{':' if hit else '-'}{n}{':' if hit else '-'}{text}"
                    length = len((entry + "\n").encode("utf-8", errors="replace"))
                    if output_bytes + length > MAX_SEARCH_OUTPUT_BYTES - 2048:
                        truncated = True
                        stop = True
                        break
                    results.append(entry)
                    output_bytes += length
                    emitted_until = max(emitted_until, n)
                if stop:
                    break
                previous.append((number, line.rstrip("\r\n")))
            # 枚举已截断仍可提供已知范围；结果/扫描上限则立即停止，避免扩大处理。
            if stop:
                break
        footer = {
            "coverage": counts,
            "skip_reasons": reasons,
            "truncated": truncated,
            "enumeration_truncated": enumeration_cut,
            "shortened_lines": shortened_lines,
            "ignored_files": "not enumerated",
            "include_hidden": arguments.include_hidden,
            "include_ignored": arguments.include_ignored,
        }
        lines = results or ["no matches"]
        if truncated:
            lines.append("[truncated: enumeration, scan, result or output limit reached]")
        if counts["skipped"]:
            lines.append(f"[skipped unreadable files: {counts['skipped']}]")
        lines.append(json.dumps(footer, ensure_ascii=False))
        return "\n".join(lines)
