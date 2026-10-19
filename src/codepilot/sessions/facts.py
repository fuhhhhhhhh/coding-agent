"""从控制器事件保留带版本的执行记录，不把模型摘要当作验证结论。"""

import re
from typing import Any

from codepilot.contracts.run import RunObservation


def execution_fact(
    observation: RunObservation, action: dict[str, Any], stamp: str
) -> dict[str, Any]:
    """提取受控工具结果及来源 ID；零退出只证明命令成功，不证明任务通过。"""
    result = observation.result
    assert result is not None
    match = re.match(r"exit_code=(-?\d+)\n", result.content) if action["name"] == "shell" else None
    return {
        "run_id": observation.run_id,
        "sequence": observation.sequence,
        "call_id": result.call_id,
        "tool": action["name"],
        "outcome": "error" if result.is_error else "success",
        "exit_code": int(match[1]) if match else None,
        "workspace_stamp": stamp,
        "source": "controller_tool_result",
        "independent_verdict": "not_run",
    }


def annotate_validity(records: list[dict[str, Any]], stamp: str) -> list[dict[str, Any]]:
    """按当前工作区版本标记旧记录失效，不删除历史上确实发生过的执行。"""
    return [{**record, "current": record["workspace_stamp"] == stamp} for record in records]
