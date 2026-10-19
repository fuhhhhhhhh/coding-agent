"""从工具声明、调用参数和执行后端生成可复核的 Action。"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from codepilot.contracts import ToolCall
from codepilot.permissions.models import Action
from codepilot.tools.base import ToolSpec
from codepilot.tools.process import workspace_environment


def build_action(spec: ToolSpec, call: ToolCall, backend: object | None) -> Action:
    """生成不含参数原文的 Action 摘要，并为执行前复核计算指纹。"""
    arguments = call.arguments
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    cwd_value = getattr(backend, "root", None)
    cwd = str(cwd_value) if isinstance(cwd_value, Path) else str(cwd_value or "<unknown>")
    backend_id = str(getattr(backend, "backend_id", "unknown-backend"))
    backend_kind: Literal["local", "container", "unknown"] = (
        "container" if getattr(backend, "is_container", False) else "local"
    )
    if backend is None:
        backend_kind = "unknown"
    identity = None
    if isinstance(cwd_value, Path):
        info = cwd_value.stat()
        identity = (info.st_dev, info.st_ino)
    summary = _argument_summary(arguments)
    # Local 进程具备宿主联网能力；不能把未注入网络参数误写成断网保证。
    network = backend_kind != "container" or getattr(backend, "network", "none") != "none"
    environment_names = tuple(sorted(workspace_environment()))
    fingerprint = _fingerprint(
        spec.name,
        spec.effect,
        call.call_id,
        digest,
        cwd,
        backend_kind,
        backend_id,
        spec.resources,
        spec.provenance,
        identity,
        network,
        environment_names,
    )
    return Action(
        tool_name=spec.name,
        effect=spec.effect,
        call_id=call.call_id,
        arguments_digest=digest,
        argument_summary=summary,
        cwd=cwd,
        backend=backend_kind,
        backend_id=backend_id,
        resources=spec.resources,
        fingerprint=fingerprint,
        workspace_identity=identity,
        network=network,
        environment_names=environment_names,
    )


def _fingerprint(*values: object) -> str:
    """对审批关键字段做稳定编码，避免 Python repr 差异影响复核。"""
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _argument_summary(arguments: dict[str, Any]) -> str:
    """生成有限且脱敏的终端参数摘要，不把命令或秘密完整回显。"""
    parts: list[str] = []
    for key, value in arguments.items():
        lowered = key.lower()
        if any(word in lowered for word in ("key", "token", "secret", "password", "credential")):
            rendered = "<redacted>"
        elif key == "command":
            rendered = "<shell command; see digest>"
        elif key in {"data", "arguments"}:
            rendered = "<private input; see digest>"
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered[:120]}")
    return ", ".join(parts)[:500]
