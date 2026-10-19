"""显式 Rootless Docker 验证安装级无模型测试与正式评测的组合路径。"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_b5 import fixture, provider

from codepilot.app.operations import test_command as run_test_command
from codepilot.config import BackendConfig
from codepilot.evals.product import ProductHarness
from codepilot.permissions import ScriptedApproval

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="需要显式 Rootless Docker 开关"
)


def test_command_and_independent_product_trial_inside_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零模型命令与正式 Agent 各用真实 Docker，隐藏材料在新容器中验收。"""
    config, suite = fixture(tmp_path)
    config = config.model_copy(
        update={"backend": BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0")}
    )
    provider(monkeypatch, "repair")

    async def scenario() -> None:
        """先确定直接命令不经过 Provider，再走完整产品/独立重建验收。"""
        command = await run_test_command(
            config,
            Path(suite.tasks[0].repository),
            ["python3", "-c", "print('docker-direct')"],
            30.0,
            ScriptedApproval([True]),
        )
        assert command["command_succeeded"] and command["model_requests"] == 0
        assert "docker-direct" in command["result"]["content"]
        task = suite.tasks[0].model_copy(
            update={"verification_command": ["python3", ".codepilot-validation/verify.py"]}
        )
        result = await ProductHarness(config, tmp_path / "docker-eval", approve_tools=True).run(
            suite.model_copy(update={"repetitions": 1, "required_successes": 1, "tasks": [task]})
        )
        assert result["task_gate_passed"], result
        assert result["results"][0]["backend_closed"]
        assert (tmp_path / "docker-eval/repair-1/baseline-verification.json").is_file()
        plan = json.loads((tmp_path / "docker-eval/plan.json").read_text())
        retained = plan["environment"]["owned_image_tag"]
        assert retained.startswith(f"codepilot-eval-{os.getuid()}-")
        assert result["owned_resources_released"]
        assert (
            subprocess.run(
                ["docker", "image", "inspect", retained], capture_output=True, timeout=10
            ).returncode
            != 0
        )
        trial = result["results"][0]
        assert trial["before"]["calc.py"]["mode"] == trial["after_changes"]["calc.py"]["mode"]

    asyncio.run(scenario())
