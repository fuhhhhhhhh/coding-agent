# CodePilot

逐阶段实现 Python 终端 Coding Agent。当前已有统一 `codepilot run` 产品入口，并完成非流式与流式 Agent 循环、六个基础工具、权限审批、
Local 评测纠错、脱敏 Trace、Rootless Docker 沙箱和两层上下文治理，并已完成一次受限的
DeepSeek 流式只读闭环验证。
未复用 CoreCoder 代码；它只是用户已学习的背景项目。

当前仓库属于“产品主链和部分真实后端已验证”，尚未达到成熟终端产品。当前首要缺口是任务级
Git worktree 和可审查补丁交付；受控执行面、上下文完整计量、持久会话、Provider 可靠性与扩展系统也仍需完成。后续成熟度门槛与实施顺序见
[产品化路线](docs/PRODUCTIZATION_PLAN.md)。

需要 Python 3.11+ 和 uv。在本目录执行：

```bash
uv sync --locked
uv run --locked codepilot run "检查当前工作区" --workspace . \
  --config codepilot.product.example.toml --session-id demo-session \
  --task-id demo-task --run-id demo-run
uv run --locked python -m codepilot.demo codepilot.example.toml
uv run --locked python -m codepilot.m02_demo
uv run --locked python -m codepilot.m03_demo
uv run --locked python -m codepilot.m04_demo
uv run --locked python -m codepilot.m05_demo
uv run --locked python -m codepilot.m06_demo
uv run --locked python -m codepilot.m07_demo
uv run --locked python -m codepilot.m08_demo
uv run --locked python -m codepilot.m10_demo
# 显式执行一次有预算的真实 DeepSeek 摘要
uv run --locked --env-file .env python -m codepilot.m10_demo --live --live-timeout 600
# Flash 长时间排队时，可显式选择另一个官方模型做受限验收
uv run --locked --env-file .env python -m codepilot.m10_demo --live \
  --live-model deepseek-v4-pro --live-timeout 600
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build --wheel
```

M01 演示输出文本和工具请求两行 JSON，不执行工具。
M02 演示需在仓库根目录运行，将 examples/fixture_repo 复制到临时目录，
确认初始测试失败，再执行脚本中的读文件、写修复、运行真实 unittest。
同时验证错误反馈、停止上限、超时和取消等故障，保存 11 组 JSONL 到 docs/evidence。
M03 演示让 Fake Provider 通过通用注册表调用六个基础工具，并保存参数错误、路径越界、
编辑歧义和二进制读取失败轨迹。文件工具限制在显式工作区；Shell 有固定 cwd、当前进程取消、
超时和输出上限，但权限审批和系统隔离仍属于后续阶段。
M04 演示文件哈希冲突、原子编辑 diff 和 Shell 超时；写入失败不会留下半成品，POSIX 进程组清理
会终止命令启动的运行中后代。
M05 演示只读拒绝、审批取消、审批通过和未实现容器模式的明确拒绝；审批层记录完整工具参数、决策和结果，
拒绝或取消发生在工具执行前。CLI 审批只批准明确输入的 y/yes，离线验收使用 fake provider 和脚本化审批。
M06 演示首次候选未通过独立验证后接收反馈并修正，另有空补丁、验证器错误和验证器超时分类；每项任务从干净临时副本开始，
独立验证代码不在 Agent 工作区内，正式 Harness 要求指定工作区外 Trace 目录。
M07 演示分片工具调用、审批、断流和规范化回放；流式参数完整校验后才执行，JSONL 与摘要使用 0600 权限。
M08 演示独立读取受限并发、同路径写入串行和排队取消；权限先于调度，Shell 与未知资源工具保守独占，
结果按调用 ID 和原始顺序回传。
M09 使用用户级 Rootless Docker 验证同工作区、断网、资源限制、取消和遗留清理。
M10 演示 110KB 工具输出落盘、错误线索与范围读取，并以完整工具回合为单位自动或手动压缩；
规范历史与 Provider prompt 视图分离，摘要用量和压缩前后估算可观察。`--live` 只发送合成历史，
固定一次 DeepSeek 请求和输出上限；服务端长时间排队时可显式放宽 live 总时限，生产摘要仍使用较短默认值。
M01—M06 使用 fake provider；M07 另完成受限真实 DeepSeek 验证。P1 的统一入口使用 fake 完成
Local 与 Rootless Docker 闭环，并通过 OpenAI-compatible 网关使用 `qwen3.5-flash` 完成真实流式
只读工具闭环，以及在临时仓库中的编辑、Shell 审批、测试和故障恢复闭环。真实密钥只从配置指定的
环境变量读取，不写入 TOML、Trace、容器或仓库。

M02 验收时 Linux 上 Python 3.11.16、3.12.12、3.14.7 均通过 66 个测试；
M03 通过 88 个测试，M04 通过 93 个测试，M05 通过 98 个测试，M06 通过 102 个测试，
M07 审查修正后通过 121 个测试，M08 当前环境通过 130 个测试，M10 当前环境通过
最新测试数量与真实环境证据见
[docs/STATUS.md](docs/STATUS.md) 和 `docs/evidence/`。
M02 wheel 构建通过，本轮未重复独立安装；M01 独立安装记录保留。
Windows/macOS、Python 3.13 尚未验证；真实模型只覆盖 DeepSeek 非推理流式只读闭环。

为便于机制学习，项目要求每个 Python 函数和方法都有中文 docstring，核心条件、异常、
停止与资源清理分支使用中文注释解释设计原因；代码标识符和协议术语可保留英文。
`tests/test_documentation.py` 会扫描源码、测试和 fixture，防止后续新增无说明函数，
或者重新引入纯英文 docstring 和行注释。

进度见 [docs/STATUS.md](docs/STATUS.md)，
产品化门槛见 [docs/PRODUCTIZATION_PLAN.md](docs/PRODUCTIZATION_PLAN.md)，
当前机制与取舍见 [docs/design/P1.md](docs/design/P1.md)，
阅读路径与执行解释见 [docs/learning/P1.md](docs/learning/P1.md)。
按用户要求不再留动手题；原 M01 命名修改已直接实现并验证。
## M07：流式与 Trace

`uv run --locked python -m codepilot.m07_demo` 离线演示分片工具调用、审批、断流与规范化回放。
默认轨迹写入 `.codepilot-traces/`，只保存元数据，费用未知不按零处理。

真实 DeepSeek 测试需要显式授权和 `DEEPSEEK_API_KEY`：
`uv run --locked --env-file .env python -m codepilot.m07_demo --live`。
每次启动最多三次请求、每次最多 256 输出 Token，关闭推理与自动重试。
本轮实际两次请求、一次只读工具，最小闭环通过；这不是完整编程任务评测。

阅读：[机制与边界](docs/design/M07.md)、[阅读说明](docs/learning/M07.md)。
