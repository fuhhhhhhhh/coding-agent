"""有界扫描文本全版本并生成可校验字节游标，长行不会被默默跳过。"""

import codecs
import hashlib
import json
from pathlib import Path

from codepilot.tools.base import ToolFailure


def read_page(
    path: Path,
    *,
    start_line: int,
    line_count: int,
    offset: int | None,
    max_bytes: int,
    scan_bytes: int,
    expected_sha256: str | None,
) -> str:
    """扫描全文哈希及行数，只保留请求窗口；版本漂移和非法 UTF-8 明确拒绝。"""
    before = path.stat()
    # 字节位置只有和读取版本绑定才是可续读游标，不能默默跨版本拼接正文。
    if offset is not None and expected_sha256 is None:
        raise ToolFailure("invalid_cursor: expected_sha256 is required for continuation")
    # 普通工具不能靠分页绕过全文扫描预算，也不读取特殊设备。
    if not path.is_file() or before.st_size > scan_bytes:
        raise ToolFailure("file_too_large: page scan limit exceeded or not a regular file")
    if offset is not None and start_line != 1:
        raise ToolFailure("invalid_cursor: offset cannot combine with start_line or exceed EOF")
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    selected = bytearray()
    position, line, newlines = 0, 1, 0
    first_offset: int | None = None
    first_line: int | None = None
    last = b""
    try:
        with path.open("rb") as stream:
            # readline 的长度限制使巨大单行也只占一小段内存。
            while chunk := stream.readline(65536):
                if position + len(chunk) > scan_bytes:
                    raise ToolFailure("file_too_large: page scan limit exceeded")
                if b"\0" in chunk:
                    raise ToolFailure("binary_file: NUL byte detected")
                decoder.decode(chunk)
                digest.update(chunk)
                skip = max(0, (offset or 0) - position)
                eligible = line >= start_line and skip < len(chunk)
                if eligible and first_line is None:
                    first_line, first_offset = line, position + skip
                if (
                    eligible
                    and first_line is not None
                    and line < first_line + line_count
                    and len(selected) < max_bytes
                ):
                    selected.extend(chunk[skip : skip + max_bytes - len(selected)])
                position += len(chunk)
                newlines += chunk.count(b"\n")
                line += chunk.count(b"\n")
                last = chunk[-1:]
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as error:
        # 其他区域的坏编码也不能被局部页面掩盖成可靠全文版本。
        raise ToolFailure("encoding_error: file is not valid UTF-8") from error
    after = path.stat()
    identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in identity):
        raise ToolFailure("conflict: file changed during paginated read")
    sha = digest.hexdigest()
    if expected_sha256 is not None and sha != expected_sha256.lower():
        raise ToolFailure("conflict: page cursor belongs to another file version")
    if offset is not None and offset > position:
        raise ToolFailure("invalid_cursor: offset exceeds EOF")
    try:
        content = selected.decode("utf-8")
    except UnicodeDecodeError as error:
        # 截断只能发生在末尾；从字符中间开始的游标必须拒绝。
        if error.reason != "unexpected end of data" or error.end != len(selected):
            raise ToolFailure("invalid_cursor: offset must be a UTF-8 boundary") from error
        del selected[error.start :]
        content = selected.decode("utf-8")
    if not selected and first_offset is not None and first_offset < position:
        raise ToolFailure("page_limit_too_small: increase max_bytes for the next character")
    end_offset = (first_offset or 0) + len(selected)
    truncated = bool(selected) and end_offset < position
    return json.dumps(
        {
            "sha256": sha,
            "total_lines": newlines + int(bool(last) and last != b"\n"),
            "file_bytes": position,
            "start_line": first_line,
            "start_offset": first_offset,
            "end_offset": end_offset,
            "content": content,
            "truncated": truncated,
            "next_cursor": {"offset": end_offset, "expected_sha256": sha, "start_line": 1}
            if truncated
            else None,
        },
        ensure_ascii=False,
    )
