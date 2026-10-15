# CodePilot

逐阶段实现 Python 终端 Coding Agent。当前已有 `codepilot` 交互入口与 `codepilot run` 单次入口，并完成非流式与流式 Agent 循环、六个基础工具、权限审批、
Local 评测纠错、脱敏 Trace、Rootless Docker 沙箱和两层上下文治理，并已完成受限的
DeepSeek 流式只读闭环与真实上下文压缩验证。
未复用 CoreCoder 代码；它只是用户已学习的背景项目。

当前仓库属于“产品主链、真实后端和任务级 Git 交付已验证”，尚未达到成熟终端产品。当前首要缺口是
受控执行面加固；上下文完整计量、持久会话、Provider 可靠性与扩展系统也仍需完成。后续成熟度门槛与实施顺序见
[产品化路线](docs/PRODUCTIZATION_PLAN.md)。

需要 Python 3.11+ 和 uv。在本目录执行：

```bash
uv sync --locked
# 开发环境直接进入交互界面；自动读取用户级或项目级 config.yaml。
uv run --locked codepilot
# 安装为用户级命令后，在任意目标仓库直接运行 codepilot。
uv tool install --editable .
codepilot
# 脚本和 CI 仍可执行单个任务。
codepilot run "检查当前工作区" --workspace .
uv run --locked python -m codepilot.demo codepilot.example.toml
uv run --locked python -m codepilot.m02_demo
uv run --locked python -m codepilot.m03_demo
uv run --locked python -m codepilot.m04_demo
uv run --locked python -m codepilot.m05_demo
uv run --locked python -m codepilot.m06_demo
uv run --locked python -m codepilot.m07_demo
uv run --locked python -m codepilot.m08_demo
uv run --locked python -m codepilot.m10_demo
uv run --locked python -m codepilot.m11_demo
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

配置按默认值、用户级 `~/.config/codepilot/config.yaml`、工作区 `.codepilot/config.yaml`、显式
`--config`、CLI 覆盖的顺序合并；旧的 `.codepilot.toml` 继续兼容。同一层同时存在多个候选文件会
直接报错，避免用户修改了一个实际却读取另一个。`codepilot.product.example.yaml` 展示了 Qwen
网关、Rootless Docker、受控编辑和凭据文件路径。当前机器使用用户级 `config.yaml`，真实密钥保存于
权限为 `0600` 的 `~/.config/codepilot/credentials.yaml`，不会进入仓库、Trace 或容器。临时设置
`CODEPILOT_YJT_API_KEY` 时，环境变量优先于文件，适合 CI 或单次覆盖。

当前 Qwen3.5-Flash 配置按模型公开规格使用 1,000,000 Token 总上下文，并为单次回答预留
65,536 Token，因此输入侧可用预算是 934,464 Token。`max_tokens: 65536` 只是输出硬上限，短回答
不会为了填满上限而额外生成。Context 当前使用近似估算器而非 Qwen 官方 tokenizer，所以这组配置
提供保守的治理边界，尚不等于已经完成百万 Token 实际压力测试。模型规格见
[阿里云百炼 Qwen3.5-Flash 文档](https://help.aliyun.com/zh/model-studio/qwen3-5-flash)。

直接运行 `codepilot` 后，输入任务并回车即可执行；`/help` 查看提示，`/exit` 或 Ctrl-D 退出。
当前进程内多条任务共享 session ID，但各自拥有 task/run ID 和 Trace。持久历史与进程重启后的
恢复属于后续会话阶段，目前输入下一条任务不会携带上一条任务的模型消息。

默认情况下，Agent 直接修改启动目录，因此未初始化目录和带已有改动的 Git 仓库都能使用；已有改动
不会因启动任务被清除。模型通过 `shell` 使用原生 Git，可执行 status/diff/init/add/commit/branch/fetch/pull/push
等命令。Shell 属于 `process` 操作，在
`controlled-edit` 与 `ask-edits` 权限下需要审批。Docker 默认断网，所以联网远端操作需使用 Local 后端；
开放容器网络要等网络权限进入统一 Action 后再支持。

需要并行任务隔离时可传 `--worktree`，从 `base_commit` 建立 detached worktree。该模式会保留修改目录、
导出二进制补丁并在干净起点重放；它是显式工作流，不再要求用户为普通编辑手动应用补丁。项目可配置
`git_workspace.verification_command` 做固定复验。隔离模式默认也允许源仓库有改动，但这些未提交改动
不属于 `base_commit`、不会出现在任务 worktree；需要强制防止这种认知差异时设置
`require_clean_source: true`。

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
只读工具闭环，以及在临时仓库中的编辑、Shell 审批、测试和故障恢复闭环。真实密钥从临时环境变量
或权限受限的用户凭据文件读取，不写入普通配置、Trace、容器或仓库。

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
