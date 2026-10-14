# CodePilot

逐阶段实现 Python 终端 Coding Agent。当前完成非流式与流式 Agent 循环、六个基础工具、权限审批、
Local 评测纠错、脱敏 Trace、Rootless Docker 沙箱和两层上下文治理，并已完成一次受限的
DeepSeek 流式只读闭环验证。
未复用 CoreCoder 代码；它只是用户已学习的背景项目。

需要 Python 3.11+ 和 uv。在本目录执行：

```bash
uv sync --locked
uv run --locked python -m codepilot.demo codepilot.example.toml
uv run --locked python -m codepilot.m02_demo
uv run --locked python -m codepilot.m03_demo
uv run --locked python -m codepilot.m04_demo
uv run --locked python -m codepilot.m05_demo
uv run --locked python -m codepilot.m06_demo
uv run --locked python -m codepilot.m07_demo
uv run --locked python -m codepilot.m08_demo
uv run --locked python -m codepilot.m10_demo
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
规范历史与 Provider prompt 视图分离，摘要用量和压缩前后估算可观察。
M01—M06 使用 fake provider；M07 另完成受限真实 DeepSeek 验证。仓库中的故意错误 fixture 不会被修改。
基础配置入口仍只支持 fake；M07 live 演示只从进程环境读取 `DEEPSEEK_API_KEY`，不加载或提交密钥文件。

M02 验收时 Linux 上 Python 3.11.16、3.12.12、3.14.7 均通过 66 个测试；
M03 通过 88 个测试，M04 通过 93 个测试，M05 通过 98 个测试，M06 通过 102 个测试，
M07 审查修正后通过 121 个测试，M08 当前环境通过 130 个测试，M10 当前环境通过
157 个测试并显式跳过 3 个需要隔离 Docker daemon 的 live 测试。最新结果见
[docs/STATUS.md](docs/STATUS.md) 和 `docs/evidence/`。
M02 wheel 构建通过，本轮未重复独立安装；M01 独立安装记录保留。
Windows/macOS、Python 3.13 尚未验证；真实模型只覆盖 DeepSeek 非推理流式只读闭环。

为便于机制学习，项目要求每个 Python 函数和方法都有中文 docstring，核心条件、异常、
停止与资源清理分支使用中文注释解释设计原因；代码标识符和协议术语可保留英文。
`tests/test_documentation.py` 会扫描源码、测试和 fixture，防止后续新增无说明函数，
或者重新引入纯英文 docstring 和行注释。

进度见 [docs/STATUS.md](docs/STATUS.md)，
机制与取舍见 [docs/design/M10.md](docs/design/M10.md)，
阅读路径与执行解释见 [docs/learning/M10.md](docs/learning/M10.md)。
按用户要求不再留动手题；原 M01 命名修改已直接实现并验证。
## M07：流式与 Trace

`uv run --locked python -m codepilot.m07_demo` 离线演示分片工具调用、审批、断流与规范化回放。
默认轨迹写入 `.codepilot-traces/`，只保存元数据，费用未知不按零处理。

真实 DeepSeek 测试需要显式授权和 `DEEPSEEK_API_KEY`：
`uv run --locked --env-file .env python -m codepilot.m07_demo --live`。
每次启动最多三次请求、每次最多 256 输出 Token，关闭推理与自动重试。
本轮实际两次请求、一次只读工具，最小闭环通过；这不是完整编程任务评测。

阅读：[机制与边界](docs/design/M07.md)、[阅读说明](docs/learning/M07.md)。
