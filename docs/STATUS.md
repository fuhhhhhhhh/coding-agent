# 开发状态

当前里程碑：M07（实现完成）

最近通过：流式事件与本地 Trace；M01—M06 已通过

实现状态：通过离线验收及 DeepSeek 非推理流式最小真实验证

学习状态：用户报告已阅读 M01、M03、M04、M05 核心代码；M02、M03、M04、M05、M06、M07 阅读说明已交付，未验证独立掌握程度。

学习偏好：用户于 2026-10-13 明确要求不再留动手题，由 AI 直接完成修改。
原 M01 命名修改已由 AI 实施并验证，不记作用户独立贡献。

日期：2026-10-14（Asia/Shanghai）

## M07 当前通过证据

- 内部流式契约拒绝跨类型载荷并按索引隔离组装；整个回复完整校验后才允许工具执行。
- Runner 兼容完整回复和流式 Provider；记录文本/工具分片、调用就绪、工具开始/结束、审批、用量与回合/运行终态。
- 默认 JSONL 仅元数据，省略提示词、参数和正文；JSONL 与摘要均为 0600，未知 usage 与费用使用 null。
- 底层 Runner 可只保留内存事件；正式 Local Harness 强制指定工作区外 Trace 目录，每次尝试均落盘。
- 支持固定 fake 脚本回放，规范化 ID/时间比较；这不等于模型解题成功率。
- `uv run --locked pytest -q`：121 passed；Ruff、格式检查、mypy 通过。
- DeepSeek 官方 SDK（openai 2.54.0）适配；共享请求额度，关闭 SDK 自动重试。
- 用户授权小范围真实验证：DeepSeek Flash 非推理模式，三次请求上限，每次最多 256 输出 Token。
  实际两次请求、一次只读工具，最终结果正确；输入 834、输出 39 Token，未计算费用。
- 证据：`docs/evidence/M07-checks.txt`、`M07-review-fixes.txt`、`M07-live-checks.json`、`M07-traces/`。

## M06 当前通过证据

- `TaskSpec` 保存任务 ID、仓库版本标记、描述、开发测试入口和 Runner 预算；`LocalHarness.run_batch` 为每个任务复制干净工作区。
- 独立验证器运行在 Agent 工作区之外，模型提前结束不会绕过验证；验证失败反馈最多进入设定的纠错次数。
- `TrialResult` 区分 success、unresolved、timeout、unevaluable 和 infrastructure_error，并保存变更文件与 unified diff。
- 覆盖首次失败后纠错成功、空补丁、验证器无法启动、验证器超时和尝试预算耗尽。
- `uv run --locked pytest -q`：102 passed；Ruff、格式检查、mypy 和 diff 检查通过。
- `uv run --locked python -m codepilot.m06_demo`：首候选失败、第二次验证通过。

## M05 当前通过证据

- `PermissionEngine` 支持 allow/deny/ask、按工具/影响类型/路径前缀匹配和拒绝优先。
- 只读、逐次审批、受控编辑三个预设已实现；审批绑定完整 ToolCall 参数，取消或拒绝发生在工具执行前。
- `ConsoleApproval` 提供 CLI 标准输入审批；离线测试使用 ScriptedApproval，不调用真实终端。
- `PermissionAudit` 记录每次判定、审批和结果；容器模式未实现时返回明确拒绝。
- `uv run --locked pytest -q`：98 passed；Ruff、格式检查、mypy 和 diff 检查通过。
- `uv run --locked python -m codepilot.m05_demo`：只读拒绝、审批取消、审批通过和容器模式拒绝通过。

## M04 当前通过证据

- `write_file` 与 `edit_file` 支持可选 `expected_sha256`；哈希不一致时返回 conflict，保留外部内容。
- 文件写入使用同目录临时文件、`fsync` 和 `os.replace`；替换失败时原目标不变且临时文件清理。
- `edit_file` 返回 unified diff，成功和冲突轨迹分别保存于 `M04-atomic-edit.jsonl`、
  `M04-hash-conflict.jsonl`。
- POSIX Shell 使用独立进程组；超时/取消终止进程组并等待直接进程，带子进程场景由测试覆盖。
- `uv run --locked pytest -q`：93 passed；Ruff、格式检查、mypy 和 diff 检查通过。
- `uv run --locked python -m codepilot.m04_demo`：原子编辑、哈希冲突和 Shell 超时场景通过。

## M03 当前通过证据

- ToolSpec 由执行所用 Pydantic 参数模型生成 Schema；注册表拒绝重名，在执行前拒绝缺字段、
  未知字段与错误类型，未知工具和预期失败形成配对 ToolResult。
- 六个基础工具已实现：read_file、write_file、edit_file、glob、search、shell；新增工具无需修改 Runner。
- 文件工具统一拒绝绝对路径、父目录和符号链接；读取区分二进制、编码及大小错误；歧义编辑不写文件。
- glob/search 有排除目录、数量与输出限制；Shell 固定 cwd，保留退出码和两路输出，支持当前进程超时、
  取消和输出截断。进程树保证明确留给 M04。
- `uv run --locked pytest -q`：88 passed；Ruff、格式检查、mypy 和 diff 检查通过。
- `uv run --locked python -m codepilot.m03_demo`：六工具正常闭环及四个错误场景通过；轨迹位于
  `docs/evidence/M03-*.jsonl`。

## M02 当前通过证据

- Linux 上 CPython 3.11.16、3.12.12、3.14.7：M02 验收时各 66 个测试通过。
- 2026-10-13 中文注释补齐后：当前环境 68 passed；每个模块、类、函数和方法均有中文
  docstring，核心分支已有中文意图注释；`tests/test_documentation.py` 同时防止说明缺失及
  纯英文注释回退。
- `uv sync --locked`、Ruff 检查/格式检查、mypy、wheel 构建：通过，未新增依赖。
- `uv run --locked python -m codepilot.m02_demo`：11 个受控场景通过。
  原始输出见 `evidence/M02-checks.txt`；各场景轨迹为 `evidence/M02-*.jsonl`。
- 正常修复在临时 fixture 中读 calculator.py、修改 add、运行固定真实 unittest；
  修复前失败、修复后通过，且文件内容核对通过；仓库中的错误 fixture 保持不变。
- 覆盖未知工具/参数错误反馈、工具异常、模型请求失败、步数与工具额度限制、
  连续同签名失败、总时限、外部取消、调用 ID 重用与错配。
- 验证唯一终态、停止后无新调用、结果进入下一次请求、参数错误不写文件，
  固定挂起测试进程超时后被回收。

## M01 历史证据

- 初始仓库检查：只有 AGENTS.md、ROADMAP.md、DEVELOPMENT_GUIDE.md；无现有实现。
- `uv sync --locked`：依据 uv.lock 安装；运行时依赖仅 Pydantic，无供应商 SDK。
- 首轮 `uv run --locked pytest -q`：30 passed；见 `evidence/M01-checks.txt`。
- M01 补验：35 passed；新增契约导入边界与独立演示进程测试；见 `evidence/M01-recheck.txt`。
- `uv run --locked ruff check .`、`ruff format --check .`、`mypy`：通过。
- `uv run --locked python -m codepilot.demo codepilot.example.toml`：两条事件，
  一条文本、一条完整工具请求；保存于 `evidence/M01-demo.jsonl`。
- 验证环境：Linux，uv 0.12.21；CPython 3.11.16、3.12.12、3.14.7 各通过 35 个测试。
- `uv build --wheel`：构建成功；从 uv.lock 导出运行依赖，在临时 Python 3.11 环境安装 wheel。
  仓库外用 `python -I -m codepilot.demo` 返回两条事件，包从 site-packages 加载；
  见 `evidence/M01-wheel-demo.jsonl`。临时验证环境已清理。
- 核心契约导入只允许标准库、Pydantic 和内部 contracts；配置的三种进程级故障均非零退出，
  stdout 无事件。现有 M01 功能无需重建。

## 未完成与未验证

- M08 及以后未实现；没有并发调度、会话持久化或完整 CLI/TUI 框架。
- 未运行 Windows/macOS、Python 3.13、sdist 安装验证；真实 API 只覆盖 DeepSeek 非推理流式只读闭环。
- M01 的 wheel 安装证据只证明当时构建可安装与运行，不证明构建字节完全可复现；
  项目锁也没有完整固定隔离构建环境依赖。
- M02 Runtime 管理本次历史、跨消息 ID 配对、观察序号和终态；没有永久会话或恢复保证。
- Runner 预算为请求/工具尝试次数与总时限；DeepSeek 另有共享请求额度及单请求输出上限，没有账户费用硬上限。
  时限依赖协作式取消，不能强制中断阻塞代码。
- 终止时可留有无结果的调用；Runner 单次使用，不支持恢复重放。
- M02 本轮只构建 wheel，未重复独立安装；M01 的安装证据不能代替 M02 安装验证。
- 未验证 Windows/macOS 的进程组行为；POSIX 后代终止可能短暂留下僵尸记录；M04 旧演示轨迹在结束后写盘；M07 JSONL 逐事件追加，但不提供崩溃恢复。

当前阻碍：无。

下一增量：进入 M08，增加受限并发、资源串行和排队取消；本轮不提前实现。

