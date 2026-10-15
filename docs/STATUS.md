# 开发状态

当前里程碑：P2 任务级 Git 工作区与可审查交付待实施

最近完整通过：P1 统一产品主链真实验收；M01—M10 的阶段证据保留

实现状态：统一 `codepilot run` 已组合配置、Provider、Context、权限、调度、Local/Docker、Runner、Trace 与清理；worktree、恢复及后续成熟产品门槛未通过

学习状态：用户报告已阅读 M01、M03、M04、M05 核心代码；M02—M10 阅读说明已交付，未验证独立掌握程度。

学习偏好：用户于 2026-10-13 明确要求不再留动手题，由 AI 直接完成修改。
原 M01 命名修改已由 AI 实施并验证，不记作用户独立贡献。

日期：2026-10-15（Asia/Shanghai）

## P1 当前证据

- `codepilot run` 是正式 console script；UI 只消费 Runtime 事件和最终报告，Agent 执行仍由同一个 Runner 完成。
- `ProductConfig` 按默认、用户、项目、显式文件、CLI 覆盖的顺序递归合并；未知字段和不可能组合拒绝。普通配置只保存密钥环境变量名或凭据文件路径，缺少密钥时在状态目录和后端副作用前失败。
- `CodePilotApplication` 用 `session_id/task_id/run_id` 关联 Trace、后端审计与资源清单，并通过一个 `AsyncExitStack` 关闭 SDK 客户端、后端和 Context Artifact。
- fake Provider 在 Local 与真实 Rootless Docker 中均通过统一 CLI 完成两轮请求和一次 `glob`，退出后后端 closed、Context 临时正文已清理。
- OpenAI-compatible 适配器复用现有内部流契约并关闭 SDK 自动重试。真实网关的 `qwen3.5-flash` 完成两轮流式请求、一次只读 `glob` 和最终中文回答；输入 7,781、输出 136 Token，总耗时约 10.6 秒。
- 无子命令的 `codepilot` 现在进入持续提示符，支持多条任务、`/help`、`/exit`、EOF 和单条失败后继续；`codepilot run` 保留为脚本入口。进程内任务共享 session ID，并各自生成 task/run ID 与 Trace，尚不共享模型历史。
- 配置现在自动发现用户级 `~/.config/codepilot/config.yaml` 或项目级 `.codepilot/config.yaml`，继续兼容 TOML；CLI 只需输入任务。YAML 采用安全加载、大小限制、对象顶层和同层唯一文件检查；示例配置只保存凭据来源，不保存秘密值。
- 真实密钥可保存到固定的用户级 `~/.config/codepilot/credentials.yaml`；当前文件归属正确且权限为 `0600`。环境变量仍拥有更高优先级；凭据文件在资源创建前校验符号链接、归属、权限、大小和严格 Schema，秘密不进入普通配置、Trace、后端或结果契约。
- 真实写入复验使用一次性临时仓库和 Rootless Docker：模型首次 `edit_file` 因上下文不匹配安全失败，随后主动 `read_file`、重新精确编辑，并在人工批准后用 `shell` 执行 `python -m unittest -q`。Agent 内测试和控制器独立复验均为 1 test passed，最终文件内容核对正确；运行后无残留容器。
- 写入复验的成功 run 使用 5 次模型请求、4 次工具调用，输入 9,122、输出 371 Token。成功前的三个独立 run 遇到网关 `APIConnectionError`：一次发生在首轮请求，两次发生在首个只读工具结果后的下一轮；它们均未修改文件并完成容器清理。
- 真实运行发现网关会发送只有 index 的空 tool-call 占位项；适配层现在丢弃不推进状态的占位项，MockTransport 回归测试固定覆盖。冲突 ID、非法 JSON 和错误结束原因仍被拒绝。
- 覆盖非法配置无副作用、Provider 初始化失败清理、后端部分启动失败清理、外部取消传播和唯一 cancelled 终态。
- `uv run --locked pytest -q`：本次扩大上下文预算后的完整结果为 189 passed、3 个显式 Docker live skip；Ruff、格式检查、mypy、compileall 和 wheel 构建通过。
- `CODEPILOT_DOCKER_LIVE=1` 的 3 个 Rootless Docker 测试通过，运行后无 `io.codepilot.managed=true` 活动容器；新建 Python 3.11 环境安装 wheel 后，`codepilot run` fake 闭环通过。
- 当前用户已通过 editable `uv tool` 安装 `codepilot`；从仓库外直接启动提示符、安装后单次 fake Local 任务和交互式 fake Rootless Docker 任务均通过，退出后无受管容器残留。
- 清除 `CODEPILOT_YJT_API_KEY` 后，安装版 CLI 已从固定凭据 YAML 完成一次真实 `qwen3.5-flash` 无工具回答；返回预期文本、正常终止、stderr 为空且无容器残留。
- 用户真实快速排序任务暴露了 512 Token 输出上限：第二轮 `write_file` 参数在结束前被截断，Runtime 保持零写入但旧界面只显示 `contract_error`。现在供应商 `length` 独立分类为 `output_limit`，终端明确提示未执行半截调用。一次性空目录真实重试成功创建 `quick_sort.py`、运行模型请求的 Shell 测试，并通过控制器独立排序检查。
- Qwen3.5-Flash 用户级配置现在按公开规格使用 1,000,000 Token 总上下文、65,536 Token 最大输出和 934,464 Token 可用输入预算；Provider 与配置契约拒绝更高输出值，流式累计硬限制提高到 16 MiB。真实短请求确认中转站接受 `max_tokens=65536` 并返回预期文本；没有实际生成 64K 输出或发送 1M 输入，当前近似 Token 估算也不等同于模型 tokenizer。
- 详细设计与真实证据见 `docs/design/P1.md`、`docs/evidence/P1-live-checks.json`、`docs/evidence/P1-real-write-shell.json`、`docs/evidence/P1-credential-file.json`、`docs/evidence/P1-output-limit-recovery.json`、`docs/evidence/P1-million-context-config.json`；完整离线命令结果见 `docs/evidence/P1-checks.txt`。

## M10 当前证据

- `ContextSeed` 把仓库规则作为受保护 system 前缀、任务信息作为 user 前缀；相关代码继续通过已有 glob/search/read_file 按需装配，不预加载整个仓库。
- `ContextManager` 包装工具执行器；超过阈值的完整 ToolResult 写入 controller-owned 0600 artifact，prompt 只保留 opaque ID、来源 call ID、字符/字节范围、SHA-256、有界摘要与头尾、最多八条中段错误线索和分段读取方法。
- `read_context_artifact` 只接受当前 store 登记的随机 ID 和有界字符范围；未知、已清理或越界引用形成配对错误，不能读取任意宿主路径。
- 每次 Provider 请求前估算输入并预留回答预算；超阈值时保留初始规则/任务和近期完整回合，只摘要更旧的完整 assistant+tool 组。自动压缩与手动 `compact` 共用同一路径，RunReport 规范历史不被 prompt 摘要覆盖。
- 摘要 usage、压缩前后估算、artifact 写入/读取和降级均有元数据轨迹；默认 JSONL 不保存原始输出、摘要、参数或 prompt 正文。
- 覆盖 artifact 不存在、磁盘写入失败、错误只在 110KB 输出中段、摘要异常、摘要仍超目标、单条消息超硬预算、历史调用结果错配和自动压缩；失败不会静默删约束或发送超预算请求。
- 固定 fake 对照中，基础策略和治理策略均定位中段错误；基础 prompt 合计估算 27,657 Token，治理策略含按需读取的三轮合计 1,001，最大单轮 562。该结果是同一估算器下的控制流证据，不是供应商账单或模型成功率。
- `ModelSummarizer` 可复用现有完整或流式 Provider；确定性验收使用 fake 摘要器，真实受限复验结果单独记录，避免把模型随机输出作为核心正确性依据。完整离线命令结果见 `docs/evidence/M10-checks.txt`。
- `uv run --locked pytest -q`：158 passed、3 个显式 Docker live skip；Ruff、格式检查、mypy、compileall、wheel 构建和 diff 检查通过。
- 2026-10-15 真实 DeepSeek 复验确认认证、余额和模型可用；Flash 与 V4 Pro 共四次受限诊断都只有 keep-alive，其中两次等待完整 600 秒后以 `summary_failed:TimeoutError` 停止。用户确认 DeepSeek 服务异常，已停止继续调用。真实摘要质量、usage 和费用仍未验证，不能声称真实路径通过。

## M09 当前证据

- `ExecutionBackend` 统一工具执行、固定 argv 命令、取消、产物导出和清理；Local 后端真实验证同工作区状态、CPU 循环超时、活动命令取消和工作区外产物导出。
- Docker 后端固定启动时解析的镜像 ID，并读取 daemon 安全模式。rootful 使用宿主普通 UID/GID；rootless 使用容器 `0:0`，该身份映射为宿主普通用户，因而可写用户拥有的 bind 工作区。两者都默认断网、使用只读根层、删除全部 capability、启用 `no-new-privileges` 及 CPU/内存/PID 限制；不挂 Docker socket、不转发宿主环境。
- 权限与调度包装链可发现底层后端；`container_mode` 只在真实容器能力存在时放行，Harness 负责启动和清理被包装的后端。
- 容器启动执行完整 ToolSpec 握手；取消会 kill 整个任务容器；固定 argv 与 Shell 工具的 OOM 用 cgroup v2 `memory.events` 的增量与 SIGKILL/137 共同识别，容器主进程退出时再读取 Docker 状态；owner/expiry 标签支持清理明确过期的遗留容器。
- 后端事件不含参数、命令、环境或输出正文，可写入工作区外 0600 JSONL；产物限制来源类型、目标位置与容量。
- `IsolatedVerifier` 从可信初始源应用候选差异，再注入 Agent 未见材料；拒绝候选符号链接和预占验证目录。
- Docker fake transport 覆盖隔离参数、rootful/rootless 身份选择、工具协议、取消、OOM 和遗留清理；真实 Rootless Docker 覆盖同工作区、只读根层、零 capability、`NoNewPrivs`、CPU 超时、64 MiB 内存 OOM、断网、宿主合成凭据不可见、资源采样、产物导出、取消无残留和过期容器清理。
- 用户级 daemon 使用 `/run/user/1016/docker.sock` 与 `~/.local/share/docker`，用户 CLI 当前上下文为 `rootless`；公共 daemon 在验收前后均保持 PID 2311、`/var/run/docker.sock` 权限 `root:docker 0660`。没有使用 sudo、没有修改 `/etc/docker`、没有重启公共 Docker。
- `CODEPILOT_DOCKER_LIVE=1 uv run --locked pytest -q tests/test_workspace_docker_live.py`：3 passed；完整离线套件为 146 passed、3 个显式 live skip；Ruff、格式检查和 mypy 通过。详情见 `docs/evidence/M09-docker-environment.txt`。

## M08 当前通过证据

- `ScheduledExecutor` 使用动态路径资源、工作区宽资源和 exclusive 模式；无冲突读取受全局上限并发。
- 同路径写入 FIFO 串行，带让出点的真实文件读改写保留两次变更；Shell 和未知工具保守独占。
- Runner 只对明确声明安全并发的执行器并发，同批完成事件按真实时间产生，结果按原调用顺序和 ID 回传。
- 排队取消不会启动工具；Runner 取消会清理活动 Shell 进程组并取消队列中的后续读取。
- 调度事件记录容量等待和资源类型；动态文件路径只留在内存，脱敏 Trace 不落实际路径。
- `uv run --locked pytest -q`：130 passed；Ruff、格式检查、mypy 和 diff 检查通过。
- `uv run --locked python -m codepilot.m08_demo`：两项读取约 0.08 秒并行，同路径写入约 0.16 秒串行，排队取消未启动。

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
- `PermissionAudit` 记录每次判定、审批和结果；M05 当时尚无容器后端，因此容器模式会明确拒绝。
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

- 宿主 bind mount 和 Context ArtifactStore 尚无经验证的磁盘硬配额；输出与产物容量限制不能替代磁盘配额。M11 及以后未实现；没有会话持久化或完整 CLI/TUI 框架。
- Artifact 索引只存在当前进程，Runner 结束后由调用方决定保留或显式 cleanup；跨进程恢复和保留策略留给会话恢复阶段。
- Token 估算不是任一模型 tokenizer 的精确值，也未计入工具 Schema 等完整请求开销；真实摘要质量、真实摘要用量与成本尚未通过验证。
- 未运行 Windows/macOS、Python 3.13、sdist 安装验证；真实 API 只覆盖 DeepSeek 非推理流式只读闭环。
- M01 的 wheel 安装证据只证明当时构建可安装与运行，不证明构建字节完全可复现；
  项目锁也没有完整固定隔离构建环境依赖。
- M02 Runtime 管理本次历史、跨消息 ID 配对、观察序号和终态；没有永久会话或恢复保证。
- Runner 预算为请求/工具尝试次数与总时限；DeepSeek 另有共享请求额度及单请求输出上限，没有账户费用硬上限。
  时限依赖协作式取消，不能强制中断阻塞代码。
- 终止时可留有无结果的调用；Runner 单次使用，不支持恢复重放。
- M02 本轮只构建 wheel，未重复独立安装；M01 的安装证据不能代替 M02 安装验证。
- 未验证 Windows/macOS 的进程组行为；POSIX 后代终止可能短暂留下僵尸记录；M04 旧演示轨迹在结束后写盘；M07 JSONL 逐事件追加，但不提供崩溃恢复。

当前阻碍：DeepSeek 服务异常仍阻断 M10 真实摘要成功验收，但不阻塞后续产品化开发。任务级 Git 起点/worktree/补丁交付、持久会话及完整产品验收尚未实现。

下一增量：实施 P2，把每个 task ID 绑定固定 Git commit 和独立 worktree，增加正式 status/diff/补丁导出、干净起点重放验证和有未交付修改时的安全清理拒绝。

