"""通过正式产品入口验收两种协议的跨文件修复、结构化压缩与恢复。"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from codepilot.app import CodePilotApplication
from codepilot.config import load_product_config
from codepilot.context import ContextLimits
from codepilot.permissions import ScriptedApproval
from codepilot.sessions.context import context_command
from codepilot.sessions.migrations import maintain_database
from codepilot.sessions.store import SessionStore, workspace_stamp


async def main(arguments: argparse.Namespace) -> None:
    """每种协议全会话最多十一次请求，最终独立核对代码与版本化事实。"""
    root = Path(tempfile.mkdtemp(prefix="codepilot-b3-finish-"))
    base = load_product_config(workspace=arguments.config_context)
    # 该验收固定低成本官方组合，未知网关或其他模型不静默消耗真实额度。
    if (
        base.provider.model != "deepseek-flash"
        or (base.provider.base_url or "").rstrip("/") != "https://api.deepseek.com"
    ):
        raise ValueError("真实验收仅允许官方 deepseek-flash 配置")
    rows: list[dict[str, Any]] = []
    for protocol in ("chat-completions", "responses"):
        workspace = root / protocol
        (workspace / "catalog").mkdir(parents=True)
        (workspace / "catalog/__init__.py").write_text("")
        (workspace / "catalog/keys.py").write_text(
            "def label_key(label):\n    return label.strip().lower()\n"
        )
        (workspace / "catalog/merge.py").write_text(
            "from .keys import label_key\n\ndef merge_labels(existing, incoming):\n"
            "    return list(dict.fromkeys(existing + incoming))\n"
        )
        tests = """import unittest
from catalog.keys import label_key
from catalog.merge import merge_labels
class CatalogTest(unittest.TestCase):
    def test_unicode(self):
        self.assertEqual(label_key(' Straße '), 'strasse')
    def test_merge(self):
        self.assertEqual(
            merge_labels([' Alpha ', 'Straße'], ['ALPHA', 'STRASSE', '', ' Beta ']),
            ['Alpha', 'Straße', 'Beta'])
"""
        (workspace / "test_catalog.py").write_text(tests)
        (workspace / "user-note.txt").write_text("保留用户未提交笔记\n")
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        config = base.model_copy(
            update={
                "provider": base.provider.model_copy(
                    update={"protocol": protocol, "max_tokens": 4096, "max_requests": 7}
                ),
                "runtime": base.runtime.model_copy(
                    update={"max_steps": 7, "timeout_seconds": 240.0}
                ),
                "sessions": base.sessions.model_copy(update={"max_requests": 11}),
                "state_directory": root / "state",
            }
        )
        identity = "finish-" + protocol
        result = await CodePilotApplication(
            config, workspace, approval=ScriptedApproval([True] * 20)
        ).run(
            "修复 catalog 中两个函数。label_key 用 Unicode casefold 并去除两端空白；"
            "merge_labels 合并已有和新增标签，忽略空白项，按 label_key 去重，"
            "保留首次出现的去空白原文及顺序，必须复用 keys.py 的函数。"
            "先运行 python -m unittest -q 看到失败，修改后复测。"
            "不改测试和 user-note.txt，不提交 Git，不访问网络。",
            session_id=identity,
        )
        public = subprocess.run(
            [sys.executable, "-m", "unittest", "-q"], cwd=workspace, capture_output=True
        )
        hidden = subprocess.run(
            [
                sys.executable,
                "-c",
                "from catalog.merge import merge_labels; "
                "from catalog.keys import label_key; "
                "assert label_key(' Σ ') == label_key('ς'); "
                "assert merge_labels([],[' ', '\\t','Σ','ς','Z',' z ']) == ['Σ','Z']; "
                "assert merge_labels(['X'],[]) == ['X']",
            ],
            cwd=workspace,
            capture_output=True,
        )
        with SessionStore(root / "state", identity, create=False) as stored:
            history = stored.history()
            requests = stored.get("request_attempts")
            facts = stored.facts_view(workspace)
        row = {
            "protocol": protocol,
            "session": identity,
            "workspace": str(workspace),
            "model_claim": next(
                (
                    m.content
                    for m in reversed(result.report.messages)
                    if m.role == "assistant" and m.content
                ),
                "",
            ),
            "runtime": result.report.reason,
            "detail": result.report.detail,
            "requests_repair": requests,
            "tool_results": [
                {
                    "call_id": m.tool_result.call_id,
                    "error": m.tool_result.is_error,
                    "preview": m.tool_result.content[:240],
                }
                for m in history
                if m.tool_result
            ],
            "independent": {
                "public_tests": public.returncode == 0,
                "hidden_tests": hidden.returncode == 0,
                "tests_unchanged": (workspace / "test_catalog.py").read_text() == tests,
                "note_unchanged": (workspace / "user-note.txt").read_text()
                == "保留用户未提交笔记\n",
                "backend_closed": result.resources.backend_closed,
            },
            "controller_facts": facts,
        }
        rows.append(row)
        print(
            json.dumps(
                {
                    "protocol": protocol,
                    "runtime": row["runtime"],
                    "requests": requests,
                    "independent": row["independent"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not all(row["independent"].values()) or row["runtime"] != "completed":
            continue
        limits = ContextLimits(
            max_prompt_tokens=16000,
            response_reserve_tokens=4096,
            compact_trigger_tokens=2600,
            compact_target_tokens=2400,
            recent_rounds=1,
        )
        compact_config = config.model_copy(
            update={
                "context": limits,
                "provider": config.provider.model_copy(update={"max_requests": 2}),
            }
        )
        before = workspace_stamp(workspace)
        compacted = await CodePilotApplication(compact_config, workspace).run(
            "/compact", session_id=identity, compact_only=True
        )
        row["compact"] = {
            "reason": compacted.report.reason,
            "detail": compacted.report.detail,
            "steps": compacted.report.steps,
            "tools": compacted.report.tool_calls,
            "workspace_unchanged": before == workspace_stamp(workspace),
        }
        row["context"] = context_command(root / "state", identity, "show")
        configuration = root / (protocol + ".yaml")
        configuration.write_text(
            "provider:\n  protocol: " + protocol + "\n"
            "  max_tokens: 4096\n"
            "  max_requests: 2\n"
            "sessions:\n"
            "  max_requests: 11\n"
            "context:\n"
            "  max_prompt_tokens: 16000\n"
            "  response_reserve_tokens: 4096\n"
            "  compact_trigger_tokens: 2600\n"
            "  compact_target_tokens: 2400\n"
            "  recent_rounds: 1\n"
        )
        resumed = subprocess.run(
            [
                sys.executable,
                "-m",
                "codepilot.ui.cli",
                "resume",
                identity,
                "根据已有事实概括修改与测试，区分命令成功和独立需求验收，不调用工具。",
                "--workspace",
                str(workspace),
                "--state-directory",
                str(root / "state"),
                "--config",
                str(configuration),
                "--jsonl",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(os.environ),
        )
        events = [json.loads(line) for line in resumed.stdout.splitlines() if line.startswith("{")]
        final: dict[str, Any] = next(
            (event for event in events if event.get("kind") == "product_result"), {}
        )
        row["resume"] = {
            "returncode": resumed.returncode,
            "reason": final.get("reason"),
            "tools": final.get("tool_calls"),
            "backend_closed": final.get("backend_closed"),
            "workspace_unchanged": before == workspace_stamp(workspace),
            "answer": final.get("answer"),
        }
        row["reset"] = context_command(root / "state", identity, "reset")
        with SessionStore(root / "state", identity, create=False) as stored:
            row["final_budget"] = stored.get("provider_budget")
            row["total_requests"] = stored.get("request_attempts")
        row["backup"] = maintain_database(root / "state" / identity)
        print(
            json.dumps(
                {
                    "protocol": protocol,
                    "compact": row["compact"],
                    "resume": {k: v for k, v in row["resume"].items() if k != "answer"},
                    "total_requests": row["total_requests"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    target = arguments.output
    target.write_text(
        json.dumps(
            {
                "root": str(root),
                "date": "2026-10-19",
                "model": base.provider.model,
                "endpoint": base.provider.base_url,
                "maximum_requests": 22,
                "results": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )

    # 失败也先保存证据，再以非零状态退出，不能把运行脚本结束当成验收通过。
    if not all(
        all(row["independent"].values())
        and row["runtime"] == "completed"
        and row.get("compact", {}).get("reason") == "completed"
        and row.get("resume", {}).get("reason") == "completed"
        and row.get("resume", {}).get("tools") == 0
        for row in rows
    ):
        raise SystemExit("真实验收未全部通过，详情已保存")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实双协议验收；最多 22 次付费请求")
    parser.add_argument("--live", action="store_true", help="显式启用真实模型调用")
    parser.add_argument("--config-context", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("真实调用需要显式 --live")
    asyncio.run(main(arguments))
