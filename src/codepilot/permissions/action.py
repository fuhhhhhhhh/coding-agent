"""从工具声明、调用参数和执行后端生成可复核的 Action。"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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
    secrets: tuple[str, ...] = ()
    network_scope: tuple[str, ...] = ()
    # 请求代理在控制器中联网，不能把容器断网状态误展示为此次调用不联网。
    if spec.name == "http_request":
        network = True
        raw = arguments.get("url")
        try:
            target = urlsplit(raw if isinstance(raw, str) else "")
            network_scope = (f"https://{target.hostname or '<invalid>'}:443",)
        except ValueError:
            network_scope = ("<invalid>",)
        ref = arguments.get("secret_ref")
        secrets = (ref,) if isinstance(ref, str) and ref else ()
    elif spec.name == "shell" and arguments.get("network") is True:
        network = True
        network_scope = spec.network_scope
    # HTTPS 代理不启动进程或继承环境，不能把 Shell 的环境白名单展示成此次注入。
    environment_names = (
        () if spec.name == "http_request" else tuple(sorted(workspace_environment()))
    )
    # 原生代理的固定环境名也属于审批内容，值仅是容器回环地址且不含秘密。
    if spec.name == "shell" and arguments.get("network") is True:
        environment_names = tuple(
            sorted(
                set(environment_names)
                | {
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "NO_PROXY",
                    "no_proxy",
                    "GIT_TERMINAL_PROMPT",
                }
            )
        )
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
        network_scope,
        secrets,
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
        network_scope=network_scope,
        secrets=secrets,
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
