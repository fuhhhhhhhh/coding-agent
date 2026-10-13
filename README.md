# CodePilot

逐阶段实现 Python 终端 Coding Agent。当前完成通用工具注册、六个基础工具和独立权限审批层，
可在受控临时工作区演示读、写、编辑、发现、搜索、Shell，以及 allow/deny/ask 权限决策；尚未接入真实模型。
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
独立验证代码不在 Agent 工作区内。
全程使用 fake provider，未调用真实模型；仓库中的故意错误 fixture 不会被修改。
配置只支持 fake；省略配置路径使用默认值，显式路径不存在则报错。
`.env.example` 仅说明无需密钥，程序不加载 `.env`。

M02 验收时 Linux 上 Python 3.11.16、3.12.12、3.14.7 均通过 66 个测试；
M03 通过 88 个测试，M04 通过 93 个测试，M05 通过 98 个测试，M06 当前环境通过 102 个测试。检查记录见
[docs/evidence/M06-checks.txt](docs/evidence/M06-checks.txt)。
M02 wheel 构建通过，本轮未重复独立安装；M01 独立安装记录保留。
Windows/macOS、Python 3.13 和真实模型协议尚未验证。

为便于机制学习，项目要求每个 Python 函数和方法都有中文 docstring，核心条件、异常、
停止与资源清理分支使用中文注释解释设计原因；代码标识符和协议术语可保留英文。
`tests/test_documentation.py` 会扫描源码、测试和 fixture，防止后续新增无说明函数，
或者重新引入纯英文 docstring 和行注释。

进度见 [docs/STATUS.md](docs/STATUS.md)，
机制与取舍见 [docs/design/M06.md](docs/design/M06.md)，
阅读路径与执行解释见 [docs/learning/M06.md](docs/learning/M06.md)。
按用户要求不再留动手题；原 M01 命名修改已直接实现并验证。

