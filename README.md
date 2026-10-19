# CodePilot

逐阶段实现 Python 终端 Coding Agent。当前已有 `codepilot` 交互入口与 `codepilot run` 单次入口，并完成非流式与流式 Agent 循环、六个基础工具、权限审批、
Local 评测纠错、脱敏 Trace、Rootless Docker 沙箱和两层上下文治理，并已完成受限的
DeepSeek 双协议真实读写、Shell 测试、跨进程续问与上下文压缩验证。
未复用 CoreCoder 代码；它只是用户已学习的背景项目。

B1 已完成持久会话、跨进程续问、原始输出与保守协调验收。当前仓库属于“产品主链、真实后端和任务级 Git 交付已验证”，尚未达到成熟终端产品。当前首要缺口是
更完整的受控执行与工作区保护；B3 已补齐共享预算、安全重试、滚动检查点、备份迁移、版本化控制器事实及保守恢复；B4 已接入受控扩展、后台任务和只读子代理并通过受限验收；B5 已接终端维护、正式真实任务评测与安装链；B2 遗留执行边界仍阻断成熟发布。后续成熟度门槛与实施顺序见
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
直接报错，避免用户修改了一个实际却读取另一个。`codepilot.product.example.yaml` 展示了 DeepSeek
官方接口、Rootless Docker 和受控编辑。当前机器使用用户级 `config.yaml`，启动时会自动读取工作区根目录权限为 0600 的 `.env`，真实密钥通过
`DEEPSEEK_API_KEY` 注入，不进入仓库、Trace 或容器；已经存在的 shell 环境变量优先，CI 仍可直接设置。
用户级默认配置还可以回退到权限为 0600 的 `~/.config/codepilot/credentials.yaml`，因此从其他工作区
启动不需要再次 `source` 项目 `.env`。工作区 `.env` 只适合项目本地覆盖，不会覆盖已经存在的环境变量。

当前示例配置使用 DeepSeek 官方 OpenAI-compatible 接口：`https://api.deepseek.com`、
`deepseek-flash` 和 `DEEPSEEK_API_KEY`。官方 endpoint 不添加 `/v1`；配置只保存环境变量名，
不会把密钥写入仓库或 Trace。当前用户配置与示例采用 65,536 Token 单次输出上限、64 次请求、
64 步、256 次工具调用和 3600 秒任务期限；上下文保持 1,000,000 Token，回答预留 65,536。
这些是上限，不会强制模型生成长回答；SDK 协议上限和模型服务端能力仍需分别核对。
提示符和帮助延迟加载 SDK，只有提交真实任务才导入客户端；等待模型时显示 Ctrl-C 取消提示。

直接运行 `codepilot` 后会显示模型、后端、目录和会话卡片；输入栏使用 `❯` 提示，工具状态和成功/失败
使用颜色展示。输入任务并回车即可执行；`/help` 查看提示，`/exit` 或 Ctrl-D 退出。管道和 `--jsonl`
模式自动关闭颜色，便于脚本消费。
多条任务共享持久历史，各自拥有 task/run ID 和 Trace。消息与工具结果保存在私有 SQLite，
Shell/Local/Docker 原始输出在预览截断前分块保存，退出后引用仍可读取；默认保留期为 30 天。
默认 Trace 仍只保存脱敏元数据，完整对话和原文在独立的私有会话目录中。

Provider 默认为 Chat Completions。支持 Responses 的 endpoint 可以显式运行
`codepilot --protocol responses`（或配置 `provider.protocol: responses`）；当前真实验收仅覆盖
官方 DeepSeek Flash 的两协议组合，不代表其他兼容站点可用。默认最多三次安全尝试，只有
429 明确拒绝及可证明未发送的建连失败重试；流中断和未知服务端接受状态不重放。
摘要、重试及主请求共用持久预算，`sessions show` 可查看 usage 与 pending，未知费用不当作零。

交互输入 `/compact`，或在 Shell 执行 `codepilot compact SESSION_ID --workspace .`，
可主动压缩已有完整回合；它只调用无工具摘要器，不发主请求、不执行文件或 Shell 工具。
历史不足时仅检查；跨进程会优先复用来源匹配的检查点，损坏时明确停止。摘要仍可能遗漏信息，
原始消息不会被摘要替换。

```bash
codepilot sessions list
codepilot sessions show SESSION_ID              # 元数据和输出引用
codepilot sessions show SESSION_ID --messages   # 显式查看私有正文
codepilot resume SESSION_ID                     # 检查后进入续问提示符
codepilot resume SESSION_ID "根据刚才的测试继续修复"
codepilot run "继续上一个任务" --session-id SESSION_ID
codepilot sessions reconcile SESSION_ID --accept-workspace
codepilot sessions delete SESSION_ID
codepilot sessions prune                        # 仅删除过期且安全结束的会话
```

B3 已增加以下维护入口，均不调用模型：

```bash
codepilot sessions backup SESSION_ID            # 私有数据库一致性备份，不含工作区/输出
codepilot sessions migrate SESSION_ID           # v1 → v2，备份后事务升级
codepilot sessions restore SESSION_ID backup-….db  # 先备份当前库，恢复后须协调
codepilot sessions diagnose SESSION_ID          # 未知调用、资源与恢复阻碍
codepilot sessions facts SESSION_ID             # 控制器记录与当前版本适用性
codepilot context SESSION_ID                    # 消息估算、摘要覆盖点和 usage
codepilot context SESSION_ID summary            # 显式查看私有摘要
codepilot context SESSION_ID reset              # 归档撤回并暂停自动压缩
```

交互可输入 `/context [show|summary|reset]`；显式 `/compact` 重新启用压缩。撤回不会回滚文件或删除消息。
旧数据库需显式 migrate；默认新会话直接使用 v2。数据库布局版本与事件版本独立，损坏快照/事件停止恢复。
恢复备份不重置已消耗预算，不能把旧历史当作当前工作区已回滚的证明。
控制器的工具退出码与版本记录独立于模型摘要；`exit_code=0` 不等于需求独立验收通过。

命令使用同一配置/状态目录及原工作区。重开会核对目录身份、文件版本和未完成调用；用户外部编辑
或未知副作用会阻断自动继续。`reconcile` 表示用户已经核对当前工作区，控制器还会检查登记进程和容器，
仅结束未知调用的协调状态，不重放工具，也不把未知结果改为成功。删除与过期清理拒绝销毁保留中的
worktree/补丁。当前会话锁和进程身份核验支持 Linux，其他平台尚未验收。

`sessions` 配置可设置命令/task/session 输出上限、Context Artifact 上限、历史/数据库容量、共享请求数和保留期。
默认分别为 128 MiB / 256 MiB / 1 GiB 输出、32 MiB Context Artifact、16 MiB 历史、64 MiB 数据库、
1000 次会话请求和 30 天保留；`prune` 需要显式运行，不在后台定时删除。输出容量限制不能代替工作区磁盘硬配额。
原始输出可能含敏感内容，私有正文应按需查看和删除；不宣称任意秘密都能被自动识别。

默认情况下，Agent 直接修改启动目录，因此未初始化目录和带已有改动的 Git 仓库都能使用；已有改动
不会因启动任务被清除。模型通过 `shell` 使用原生 Git，可执行 status/diff/init/add/commit/branch/fetch/pull/push
等命令。Shell 属于 `process` 操作，在
`controlled-edit` 与 `ask-edits` 权限下，严格识别的 `pwd`、`ls`、`git status/log` 等查询会自动放行，
复合 Shell、写入、网络和未知命令仍需要审批。Docker 默认断网，所以联网远端操作需使用 Local 后端；
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
Local 与 Rootless Docker 闭环，并通过 DeepSeek 官方 OpenAI-compatible 接口使用 `deepseek-flash` 完成真实流式
只读工具闭环，以及在临时仓库中的编辑、Shell 审批、测试和故障恢复闭环。真实密钥从临时环境变量
或权限受限的用户凭据文件读取，不写入普通配置、Trace、容器或仓库。

M02 验收时 Linux 上 Python 3.11.16、3.12.12、3.14.7 均通过 66 个测试；
M03 通过 88 个测试，M04 通过 93 个测试，M05 通过 98 个测试，M06 通过 102 个测试，
M07 审查修正后通过 121 个测试，M08 当前环境通过 130 个测试，M10 当前环境通过
最新测试数量与真实环境证据见
[docs/STATUS.md](docs/STATUS.md) 和 `docs/evidence/`。
M02 wheel 构建通过，本轮未重复独立安装；M01 独立安装记录保留。
Windows/macOS、Python 3.13 尚未验证；本轮真实模型覆盖官方 DeepSeek Flash 双协议修复、压缩和恢复，支持矩阵见 docs/evidence/B3-provider-matrix.md。

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

B4 扩展与后台协作使用同一产品入口，配置示例见 [extensions.yaml](examples/extensions.yaml)。
执行扩展默认关闭；需在可信用户配置或显式 `--config` 中设置 `extensions.enabled: true`。
启用后模型可使用后台命令、只读 explore/review、受控 Hook 和已配置的本地 stdio MCP。
技能放在 `.codepilot/skills/名称/SKILL.md`，YAML 头含 name/description；先发现元数据，再按需加载正文。
技能与仓库规则不能授予执行权限。Hook/MCP 程序须在所选后端中可用，Docker 不回退宿主执行。

交互 `/tasks` 查看任务。运行中在另一个终端使用相同工作区和状态配置：

```bash
codepilot tasks list SESSION
codepilot tasks show SESSION JOB
codepilot tasks cancel SESSION JOB
codepilot tasks input SESSION JOB  # TTY 输入不回显并附换行；stdin 保留原字节
```

这些管理命令不请求模型。后台任务只归当前 run 所有，前台退出会取消并等待清理；重启不自动复活。
MCP 为逐调用独立进程，只读子代理共用父预算且不能写入、执行 Shell 或继续委派。
完整契约、验证范围和推荐阅读顺序见 [B4 设计](docs/design/B4.md) 与 [B4 阅读说明](docs/learning/B4.md)。

B5 增加日常维护和独立评测入口，均有清晰的模型调用边界：

```bash
codepilot doctor                         # 仅读诊断，不请求模型或改服务器配置
codepilot permissions                    # 查看实际权限
codepilot diff                           # 当前目录整体 Git 变化；不是单个 Agent 的贡献归属
codepilot test --timeout 120 -- python3 -m unittest  # 原执行链审批，零模型请求
codepilot resources prune SESSION        # 默认只预览
codepilot resources prune SESSION --apply  # 精确清理登记资源，不删代码，之后仍需协调未知调用
```

交互可用 `/doctor`、`/diff`、`/permissions`、`/test 命令`。脚本无 TTY 时待审批操作直接拒绝，
不能把管道里的任务文本当批准答案。审批期间工具进度会暂存，取消不会留下抢占输入的线程。
`test` 的成功只表示该命令退出 0，不冒充独立任务验收。当前 Local 是可信宿主执行，Docker 默认断网。

真实仓库任务清单见 [real-repositories.json](examples/evals/real-repositories.json)。它固定来源 commit，
在一次性副本中注入已知回归与用户笔记，覆盖两种 Python 布局、跨模块修复、公开失败日志和隐藏验证。
这些是注入回归，不是宣称发现或修复了上游当前 bug。评测材料是可信执行配置，请只运行审查过的清单。
仓库获取是控制器显式下载，`--approve-tools` 明确授予本次可信试验有限批量审批：

```bash
codepilot eval examples/evals/real-repositories.json \
  --config examples/evals/config.yaml --output /tmp/codepilot-evaluation \
  --allow-download --approve-tools
codepilot eval-compare /tmp/first/report.json /tmp/second/report.json
```

输出目录不得复用：中断留下 started 记录，不能自动重放未知副作用。配置/任务/镜像等漂移时拒绝混合比较。
默认保留候选、基线、会话、隐藏验收和全部失败。每次主/摘要/子请求共用同一账本，报告列出实际调用和未知用量。
首次使用真实模型需要已有有效用户配置和凭据；本项目不把密钥放进评测清单或 CI。

目前 B5 的任务验收与成熟发布分别判定：`task_gate_passed` 不能替代 `release_ready`。
网络/秘密、硬磁盘配额、worktree+Docker Git 和安全撤销仍是成熟发布阻断，不会由“样本通过”自动消失。
详细机制、支持范围及推荐阅读入口见 [B5 设计](docs/design/B5.md)、[B5 阅读说明](docs/learning/B5.md)。
