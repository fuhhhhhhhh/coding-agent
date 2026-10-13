# CodePilot

逐阶段实现 Python 终端 Coding Agent。当前完成 M02 非流式单 Agent 循环，
可在受控临时 fixture 中演示读、改、测；尚不是可供真实模型使用的通用 Coding Agent。
未复用 CoreCoder 代码；它只是用户已学习的背景项目。

需要 Python 3.11+ 和 uv。在本目录执行：

```bash
uv sync --locked
uv run --locked python -m codepilot.demo codepilot.example.toml
uv run --locked python -m codepilot.m02_demo
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
全程使用 fake provider，未调用真实模型；仓库中的故意错误 fixture 不会被修改。
配置只支持 fake；省略配置路径使用默认值，显式路径不存在则报错。
`.env.example` 仅说明无需密钥，程序不加载 `.env`。

Linux 上 Python 3.11.16、3.12.12、3.14.7 均通过 66 个测试。
检查记录见 [docs/evidence/M02-checks.txt](docs/evidence/M02-checks.txt)。
M02 wheel 构建通过，本轮未重复独立安装；M01 独立安装记录保留。
Windows/macOS、Python 3.13 和真实模型协议尚未验证。

为便于机制学习，项目要求每个 Python 函数和方法都有中文 docstring，核心条件、异常、
停止与资源清理分支使用中文注释解释设计原因；代码标识符和协议术语可保留英文。
`tests/test_documentation.py` 会扫描源码、测试和 fixture，防止后续新增无说明函数，
或者重新引入纯英文 docstring 和行注释。

进度见 [docs/STATUS.md](docs/STATUS.md)，
机制与取舍见 [docs/design/M02.md](docs/design/M02.md)，
阅读路径与执行解释见 [docs/learning/M02.md](docs/learning/M02.md)。
按用户要求不再留动手题；原 M01 命名修改已直接实现并验证。

