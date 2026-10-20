# 开发状态

当前开发主题：B2 原生命令受控联网已在 Linux Rootless/bind 与匿名 Git/pip 范围验收。B3 限容恢复、请求级 HTTPS 已有证据；私有 CLI 凭据/短期秘密、限容网络组合及 B5 最终支持矩阵仍为发布缺口。

已验收范围：B1 可靠执行与持久会话基础已通过受限真实验收；B2 基础与安全交付回补已在明确范围验证，B3 在 Linux/官方 DeepSeek Flash 双协议范围通过受限验收，B4 在 Linux Local/Rootless Docker/官方 DeepSeek Flash Chat 范围通过受限验收，B5 终端操作与正式评测已在明确范围验收；成熟发布仍被 B2 遗留项阻断

最近通过：B2 真实仓库分页/检索覆盖、生产读版本、多文件部分失败和显式 Rootless 限容副本的交付/撤销；此前文件工具归属撤销、审批排队后复核、Docker 独立 Git 与同后端重放；B5 无模型维护/测试、固定真实仓库重复评测、独立判据与安装；B4 受控扩展、后台任务与只读子代理；B3 长任务、持久恢复与 Provider 可靠性的受限产品链；B1 持久会话、完整输出与保守重开；P1/P2、M01—M10 历史证据保留

实现状态：统一入口默认直写当前目录，已组合持久多轮、跨进程 resume、输出读搜与配额、SQLite 事实/索引、资源核验和保守协调；B2 已增加安全 Action 与工作区基线；B3 已接入持久尝试预算、安全有界重试、跨进程滚动检查点、主动压缩，以及 Chat Completions / Responses 双协议真实修复；已补齐备份迁移/恢复、快照来源核验、三处真实崩溃矩阵、版本化执行事实和上下文撤回；B4 新增按需技能与作用域规则、受控 Hook、逐调用本地 stdio MCP、后台/PTY 与共享预算只读子 Runner

学习状态：用户报告已阅读 M01、M03、M04、M05 核心代码；M02—M10 阅读说明已交付，未验证独立掌握程度。

学习偏好：用户于 2026-10-13 明确要求不再留动手题，由 AI 直接完成修改。
原 M01 命名修改已由 AI 实施并验证，不记作用户独立贡献。

日期：2026-10-20（UTC）

## 最新 B2：原生命令受控联网（2026-10-20）

- 本轮完整主题为可信配置、同一 shell 显式 network 参数、Action/审批/调度复核、专有临时容器、私有 CONNECT、共享额度/租期、原生 Git/pip、未知状态和崩溃维护的统一产品链。默认关闭；普通主容器、Hook/MCP/后台/只读子 Runner 继续断网。没有新增 Git/pip 包装工具。
- 仅 Linux Rootless Docker bind。每次联网命令在同一实际 bind 工作区的专有 network=none 容器执行，只有它只读挂载 owner-only socket；固定回环桥向控制器转发 CONNECT。精确域名 TCP:443、整组公开 DNS/固定数字 IP、累计连接/并发/双向字节/租期；不继承宿主代理，不回落直连。CONNECT 不检查加密路径/方法，也不强制内部是 HTTPS；TLS 校验由原生客户端负责。
- 租期在首次申请时启动；持续单向下载按双向共同活动计空闲。DNS 在可终止独立进程运行，取消独立核对 PID 已结束。监听/创建之前登记私有资源，create 返回 ID 前持久阶段覆盖不确定创建；先关代理再删整容器。主容器关闭不能掩盖 child 清理失败，未关闭/损坏租约保留并阻断 ready；resources prune 精确核验归属/镜像/ID/隔离属性/socket，默认预览。活动控制器仍拥有的活动租约拒绝接管；已结束调用的 cleanup_failed 可在持会话锁后明确回收。
- 连接前持久 network_pending；命令成功、所有连接结束且全部清理成功才结算。已发后的超限/超时/取消保留未知，不能靠 accept-workspace 清除，不自动重放。桥/Docker 故障返回固定错误类别，不回显异常原文。新增 test --network、/test --network，doctor/permissions/diagnose 可见范围与边界；真实 TTY 批准后 Git 查询成功，零模型请求。
- 最终源码 Python 3.14：450 passed /31 skipped /28.29s；仓库外 Python 3.11 非 editable wheel：450 passed /31 skipped /32.05s，详见 B2-native-network-checks.json。Ruff、215 文件格式、166 文件 mypy 通过，117 个 Python 源文件与 wheel 字节核验；最终代码提交 5234c64 的远程 Python 3.11/3.14 CI 均通过（运行 39371851106）。Rootless 联合 82 passed /215.07s，其中 32 项实际容器、50 项配置/协议/产品链确定性；最终原生容器组再验 11 passed /93.16s。全组后对下载空闲、总开关及解释器路径补回归；最终原生+HTTPS 相关 Rootless 组 65 passed /96.21s（15 项实际容器、50 项确定性），最终源码/安装计数以上述文件为准。
- 实际匿名 GitLab 查询；受控真实 TLS/git-http-backend fetch/push，宿主独立核对 remote ref 和正文；固定 idna 3.20 wheel 按 uv.lock SHA256 安装，后续独立断网容器导入；IPv4/IPv6 绕过、未授权域名/审批拒绝、桥失败、child 清理失败、实际已发后超限/超时/取消、SIGKILL 控制器后正式 prune 均验证。公开 GitHub 直连在宿主也超时，不能把宿主代理成功算作受控代理成功，失败保留在 evidence。
- 官方 DeepSeek Flash Chat：冻结总上限 12，实际 10 请求、66,378 输入/5,108 输出 Token；11 工具全成功，1 个原生联网命令、1 条 CONNECT、1,420,560 双向观察字节。Runtime completed；模型生成 inspect_repo.py 后，由新断网验证容器分别对固定公开提交（43 文件）与隐藏不同提交（44 文件）独立验收，非法目录非零，保护字节不变，Provider/network pending 为空，主/验证容器均关闭。四层事实及原始候选 SHA 分别保存；不把这些结果推广为任意任务成功率。
- 收尾解释器边界：DNS 使用启动时解析并固定的真实 Python 路径、-I -S，不再通过可被工作区替换的 .venv 软链接启动宿主进程。真实解释器位于工作区时，原生/HTTP 均在凭据/会话/容器/Trace 前拒绝；doctor 不把无效联网配置标 ready。两个新增回归验证实际替换程序未执行和零状态创建。此修正做最终确定性/安装/真实 Rootless 复验，未追加模型调用；模型证据明确对应前一代码阶段，不冒充额外模型复验。
- 镜像仍为已验证的旧不可变 worker sha256:d834e1675f8104ae5c45819c29f9295f462ffa3339b31888b20a14952d5f0e8e；本轮固定桥源码由可信控制器执行，验收的是新控制器+该镜像组合，未重新构建/声称新镜像源码字节相等。用户全局 native 默认未开启；真实用户测试仓库仍 clean、HEAD 4f04ef3。
- 当前未支持：私有 CLI 凭据、发行方短期令牌生命周期、SSH、bounded-copy 联网、rootful/Local 强制网络范围、扩展联网、自定义企业代理及跨平台。不是整个 B2/成熟发布完成，release_ready=false。下一大阶段集中秘密生命周期与 B5 支持矩阵/固定任务/安装升级复验，详见 PRODUCTIZATION_PLAN 和 ROADMAP；不重复已验收机制。
- 阅读顺序及三道理解题答案在 learning/B2-native-network，契约与目录原因在 design/B2-native-network；运行/失败证据在 B2-native-network-checks、B2-native-model、B2-native-candidate。实现通过，学习待用户确认。

## 上一轮 B2：受控 HTTPS 与凭据代理（2026-10-20）

- 完整请求级产品链：可信用户/显式配置目的地与秘密引用 → 主 Runner http_request / 零模型 fetch、/fetch → 现有 Action/审批/排队后复核 → 控制器 HTTPS → Context/会话/Trace/诊断。Docker Shell 始终 network=none。默认关闭，项目只能关闭，不允许 Local 宣称强制网络范围；不会给只读子 Runner 增加联网能力。
- 仅 HTTPS:443、精确 DNS 名/路径段范围；所有 DNS 地址必须公开单播，连接固定数字 IP、TLS SNI 和证书仍核验原域名。匿名重定向逐跳复核和扣额；认证重定向拒绝；不继承宿主代理/Cookie，不接受任意 header/body/POST，无自动重试。共享请求/响应字节/总字节/租期上限，原生 asyncio TLS 取消关闭连接，h11 处理 HTTP 边界。
- 秘密只从工作区外 owner-only 普通文件按引用读取，不能复用 Provider 文件；TLS 成功后读，O_NOFOLLOW 与 fd/读前读后身份核验，版本及配置进审批指纹。仅本次 Bearer 头使用，不向进程环境、模型、Trace/会话复制。有限正文完整收集后检查原文/base64/URL/hex 回显；认证错误正文、压缩、1xx/升级和非 UTF-8 不开放。不是任意编码泄露保证，也不替服务端签发/撤销短期令牌。
- request_sending 在写 socket 前提交，只有完整响应能清除 network_pending。已发送但结果未知会阻断再次联网、自动源交付和下次 resume；配对错误/Runtime completed 不等于会话 ready。sessions diagnose、终端与 JSON 可见，--accept-workspace 不足，需额外 --accept-network-unknown 明确接受未知且不重放，旧事实保留。
- 最终离线 Python 3.14：425 passed / 20 skipped / 23.71s；仓库外 Python 3.11 非 editable wheel：425 passed / 20 skipped / 28.44s。Ruff、206 文件格式、159 文件 mypy；wheel 113 Python 文件字节核验通过。Rootless 最终联合 71 passed / 141.76s（21 项使用实际容器，另 50 项为配置/协议/产品链确定性测试）；包括已发送但响应截断后的持久阻断、独立网络未知确认。管理容器残留 0。
- 官方 DeepSeek Flash Chat：预冻总上限 8，首次文件任务 5 请求 + 同一会话最终代码续问 2，共 7；总输入 30,148 / 输出 697 Token，Provider pending 和 network_pending 为空。首次 4 工具含 1 次引用名 none 被拒（零联网），其余 HTTPS/写/读成功；续问 1 个 HTTPS 成功。两次 Runtime completed，独立正文行完全匹配、保护文件/续问源字节不变，容器关闭。参数说明已澄清匿名引用省略/空串；没有声称额外模型 A/B 优化。
- TLS 最终复核：代理显式加载 OpenSSL 编译时系统 CA 路径，忽略 SSL_CERT_FILE/SSL_CERT_DIR/SSLKEYLOGFILE；实际测试 CA 环境不能被信任且不生成 TLS 密钥日志，公开 HTTPS 使用生产上下文重验成功。系统 CA 目录可能惰性加载，不能以初始缓存为空认定缺失。此修正后重新跑完整源码/wheel/Rootless，以上为最新计数；不额外调用模型。
- 原始失败保留：初期测试工厂错误使用 Local 后端位置参数，修正夹具接口；公开 robots.txt 无 Sitemap 的预检查在零模型请求时失败，冻结为实际 User-agent 行后再调用；完整 Docker 首轮 67 passed / 2 failed，是证书失败用例内外 monkeypatch 还原顺序污染后续连接器，保留断言并修正测试隔离后全组 69 passed；最终又增加远端未知协调门槛并复验，不删除失败或把环境替换当公开网络证据。
- Linux/Rootless 明确范围；合成秘密只在本机受控 TLS 中使用，公开 HTTPS 与真实模型没有业务秘密。最终使用既有固定 worker 镜像与新控制器组合，没有新镜像完整构建证据。Windows/macOS、真实业务认证、透明 Git/pip/SSH、远程 MCP、通用短期进程凭据、强制代理和最终 B5 发布门槛仍未验收，release_ready=false。
- 修改与目录原因、正常/故障轨迹、推荐阅读顺序及三题答案见 design/B2 和 learning/B2 最新节；B2-network-* 保存四层结果、安装、失败与检查事实。学习掌握待确认，无动手题。

## 最新 B3：限容执行的持久恢复闭合（2026-10-19）

- B3 不是尚未开始：此前已验收长任务预算、上下文压缩、双协议 Provider、备份迁移与崩溃协调。本轮按用户指定推进新限容执行模式的跨控制器恢复；“当前开发主题”和“发布阻断”分开记录。B2 网络/秘密仍未完成，不据此阻止断网恢复开发，也不据更换主题解除 release_ready=false。
- 新增 `recover SESSION RUN [--apply]` / `/recover RUN [--apply]`：默认只读预览，应用只保存产物并清理核验归属的容器，不重放工具、不自动覆盖源目录、不自动协调或标任务成功。恢复后仍 needs_reconciliation，保留未知调用与费用；显式协调、原 delivery、新任务续问沿既有链。
- create 前持久保存随机名称/backend_id、镜像/配额/目录身份；creating/created/seeding/active/exported/closed 阶段可跨控制器读取。Rootless 与容器标签/名称/镜像、私有 PID、断网/只读/能力/挂载边界独立核验；旧 run 没有绑定不能猜测恢复。客户端超时不代表 daemon 未接受，创建结果未知时不重发、不谎报 closed。
- 内容寻址归档先落盘、SHA 指针再提交、最后回收旧版本，latest 文件仅供查看。活盘先停写导出，已停止/消失只用完整旧检查点并报告 data_loss_possible；损坏归档、未知源交付、会话竞争都拒绝。Docker 控制操作/握手/归档等待有上限，stats 采样超时只跳过诊断。未交付副本不能被 sessions delete/prune 静默丢弃。
- 离线 374 passed / 17 skipped；仓库外 Python 3.11 非 editable wheel 同为 374 passed / 17 skipped。Rootless Docker 全组 17 passed / 152.31s，最后限容导入期限收尾又复验相关 6 项通过 / 67.27s，包含真实控制器被杀、新 CLI 导出清理、源版本交付、重复恢复以及真实停止 tmpfs 后只恢复旧检查点。Ruff、201 文件格式、154 文件 mypy、110 Python 文件 wheel 字节核验通过。
- 官方 DeepSeek Flash 在真实恢复后的原会话只读续问：上限 4、实际 2 请求，6,681 输入 / 432 输出 Token，Runtime completed；独立确认 a=original、marker=partial、源字节未变、后端关闭、pending 为空。旧 unknown 工具结果仍在规范历史，不把它改成成功。四层证据在 B3-bounded-recovery-live.json。
- 首轮 Rootless 1 failed / 1 passed / 333.85s：创建延迟导致 writer 屏障前超时。持久 creating 绑定后来找到确切 Created 容器并安全回收；重跑真实崩溃链 1 passed / 10.70s，再独立完成全组。新镜像构建在元数据/上下文阶段长时间等待，取消仅本轮自有客户端，没有重启公共 Docker；最终验收既有 worker/archive 镜像与新控制器组合，不冒充新镜像全源码核验。模型、工具和 Runtime 的结论不代替独立文件/容器判据。
- 当前边界：Linux/Rootless、显式恢复与协调；tmpfs 掉电/daemon 崩溃后检查点之外仍可能丢失；不重放源 applying/undoing，不保证外部 exactly-once。Windows/macOS、新镜像完整重建、真实未知 HTTP 接受状态崩溃、B2 受控网络/秘密本轮未验收。管理容器残留 0，testcodingagent 仍干净。
- 阅读顺序、轨迹和三题答案见 learning/B3 最新节；契约/实现边界见 design/B3；日志、失败、真实续问与安装证据见 B3-bounded-recovery-*。学习掌握待确认，不留动手题。

## 最新 B2：真实仓库工具与限容副本（2026-10-19）

- 本组覆盖完整产品链的读取、检索、生产版本前置条件、多文件编辑、硬容量隔离与源版本交付，不按单个技术点停顿交付；B2 整体仍未闭合。路线依旧五大阶段，基础版还剩 B2 收口、B5 最终复验两道关，不承诺固定两轮完成。
- 生产 read_file 默认分页：完整 SHA-256/总行数、有限内容窗口、绑定版本的 next_cursor；巨大单行按 UTF-8 边界续读，游标/版本/编码/扫描超限明确拒绝。已有文件写入必须有实际成功读取版本或明确 expected_sha256；读取之后用户修改会冲突。独立库 full 默认兼容，生产读取不得被旧假 Provider 的“未读就写”测试绕过。
- search 用固定原生 rg argv 遵守忽略规则，隐藏/忽略显式选择；字面文本/上下文、扫描覆盖、超大/二进制/路径跳过和返回截断均可见。长行命中提供明确片段。edit_file 最多 16 文件整批预检查、逐文件复核；按实际原/目标编码量预留日志，16 个小文件实际修改与撤销已覆盖；实际第二写失败只确认第一文件，列 applied/failed/unattempted，可审查与有条件撤销，不声称跨文件事务。
- 默认仍直写当前目录。新显式 --bounded-workspace-bytes 选择 Rootless Docker 1—64MiB 限容 tmpfs 副本，复制实际脏内容，排除源 .git/.env* 和缓存；不与 Local/rootful/worktree/可执行扩展混用。任务结束前保存并验证归档；源版本冲突只保留产物，正常结束才尝试交付。支持该副本中 Shell 的二进制/新建/删除/普通权限变化，新增零模型 delivery 预览/显式交付，沿原 changes/undo 有条件撤销。
- 控制器私有 before/after/状态/逐文件进度与会话锁、逻辑产物预算相连。交付中断保留 applying/undoing，needs_reconciliation 阻断自动重放。创建容器即登记，登记/启动失败也清理；导出失败保留精确容器及上次产物，backend_close_failed 不冒充 closed。限容容器自动过期清理和 resources prune 均拒绝静默删掉唯一产物。孤儿容器的跨控制器自动导出恢复尚未实现。
- 离线 Python 3.14：361 passed / 15 skipped；Python 3.11 仓库外非 editable wheel 同为 361 passed / 15 skipped。Ruff 全目录、198 文件格式检查、151 文件 mypy 通过；wheel 和实际镜像 109 个 Python 源文件字节独立核对。Rootless 全组 15 passed；随后补排除 worktree 的 .git 元数据文件，相关真实流程再验 4 passed（含容器不可见/源文件保留、16 文件编辑/撤销、取消/导出故障）。最终镜像与复验记录见 B2-repository-checks。资源警告/未回收异常升为失败；tmpfs 测试不是掉电耐久性证据。
- 真实官方 DeepSeek Flash：冻结两项固定真实上游注入回归，各 1 次、总上限 64 请求，实际 31；384,662 输入 / 8,928 输出 Token。Tomli 14 请求/17 工具、boltons 17 请求/21 工具；两次 Runtime completed、源交付 applied、独立隐藏验收 passed、保护文件未变、资源清理。不是“38 个工具都成功”：共 7 次工具错误含错误游标、回归复现、额外断言/查询失败，逐项保留。模型自述、工具事实、Runtime 和独立判据分别记录在 B2-repository-live.json。
- 失败与修正：旧测试假 Provider 未先读文件导致生产版本策略拒绝，修正脚本真实读取；后端工厂新增登记参数使旧故障 fixture 未进入屏障，修正 fixture 接口后恢复，不把中断运行记通过。实际 docker cp 无法导出 tmpfs，改固定归档协议。真实游标误用后补参数 Schema 说明，但没有额外模型 A/B 统计。boltons 额外 doctest 的尾部空白失败及临时 bytes 填充断言与原版行为独立比较，除源行号变化外一致；隐藏验收通过不等于全部上游测试通过。
- 当前支持边界：Docker 仍 network=none、秘密引用注入未开放；tmpfs 易失，OOM/主机故障可能丢检查点后的内容，私有产物逻辑额度不是宿主磁盘硬配额。Windows/macOS、完整 TUI、远程/有状态 MCP 和写代理/团队不在本轮通过范围。release_ready=false。下一组继续 B2 受控目的地网络/短期秘密及恢复边界，随后 B5 固定任务/故障/安装/成本复验。
- 推荐阅读顺序、一次完整轨迹和三题答案见 learning/B2 最新节；设计、目录原因与故障契约见 design/B2；证据见 B2-repository-*。实现通过不代表用户已掌握。

## 本轮 B2：安全执行与交付回补（2026-10-19）

- 正式产品链新增私有逐调用文件日志，写前 intent、修改前原始字节/模式、预期与实际后版本，成功确认后才具备撤销资格。新增 `/changes RUN`、`/undo RUN [--apply]` 与进程外 changes/undo；维护零模型、默认预览、取得原会话锁。用户后续修改、链接、日志损坏/未知意图均拒绝；撤销中断保留逐文件进度并需要协调，不声称跨文件原子事务。
- 新建文件通过 expected_absent 与同目录原子 link 防并发创建覆盖；已有文件校验版本，编辑器采用同一份已读取字节的哈希，修复原来写前重算哈希可能掩盖外部变化的问题。日志单文件 1MiB、单 run 编码日志 16MiB，意图保存失败不启动文件工具。旧 run 没有正文日志，不能追溯撤销；Shell/扩展写入不自动认领。
- 审批绑定目录设备/inode、实际网络能力、环境变量名、策略摘要和单调时钟有效期；调度取得槽后再核对，过期/策略/参数/后端漂移拒绝。显式 deny 优先于安全读白名单。仓库只可降低后端数值额度，不能取消用户请求的硬限制。
- 子进程只继承最小环境，不自动继承 Provider/代理/SSH agent；Git 默认禁用全局配置、Hook 和 fsmonitor，避免安全查询借仓库配置启动额外进程。Local 仍是可信宿主执行，Action/doctor 明确显示宿主联网能力。
- Docker --worktree 使用固定 commit 的完整独立 clone，不挂载源 .git/凭据，不用硬链接/共享 objects、移除 origin。交付比较固定 base，包含任务已经 commit 后 status 干净的修改，支持二进制补丁；控制器导出前拒绝元数据重定向并恢复安全 Git 配置，候选配置私有保留，防 filter 在宿主执行。
- 重放验收也沿原 Local/Docker 后端，验证副本在 Docker 模式自带完整 Git 元数据；容器启动纳入原会话，启动失败不回退宿主。资源未确定关闭时保留重放目录并阻断正常恢复，不能因主任务容器关闭就删除证据。
- 离线 Python 3.14：343 passed / 11 skipped，资源和未回收异常警告升为失败。Rootless Docker 全组 11 passed / 185.52s；最终隔离/新建/撤销两项再验 2 passed / 29.44s。Ruff/format/mypy、106 Python 文件 wheel 字节核验通过；Python 3.11 仓库外最终非 editable wheel：343 passed / 11 skipped（21.17s），详见 B2-delivery-checks。
- 官方 DeepSeek Flash + Docker：16 请求预算实际 6，18,540 输入 / 1,013 输出 Token，7 工具全部成功，Runtime completed；新容器独立测试通过。控制器确认用户后来编辑触发冲突、恢复任务末版后撤销成功还原原始文件、用户笔记保留，pending=0，后端关闭。模型自述、工具、Runtime、独立判据分开保存在 B2-delivery-live.json。
- 失败证据不隐藏：首轮严格离线暴露旧 SQLite 测试连接泄漏，显式关闭后修复；中断的 Docker 运行不计通过，核验并只清理一个本轮确定归属的 Created 容器后重跑。全部管理容器最终为空，testcodingagent 独立 Git 检查干净。
- 支持范围仍受限：Docker 隔离当前要求仓库根目录；默认继续直写目录。网络 none、秘密注入未开放；请求硬工作区磁盘配额明确拒绝，doctor 不报告可用。B2 整体仍未闭合，后续继续受控网络/秘密、限容任务盘、P3.5 读搜版本和批次归属，不另起 B6；release_ready=false。Windows/macOS、完整 TUI、联网和真实硬盘容量边界未验收，不将 tmpfs 当掉电证据。
- 推荐阅读与三题答案见 docs/learning/B2.md 的最新安全交付节；实现通过不代表用户已掌握。

## 本轮 B5：终端交付与真实评测（2026-10-19）

- 新入口 doctor/diff/permissions/test/resources prune/eval/eval-compare，交互 /doctor、/diff、/permissions、/test。维护不请求模型；test 复用原审批/调度/输出/会话/Trace，新增 command_requested，steps=0、真实请求为 0，命令成功不冒充独立验收。
- TTY 审批改可取消 fd 监听，非 TTY 默认拒绝；审批期间有界缓冲人类进度，JSONL 实时。启动卡片按显示宽度对齐，结果带版本与累计预算。Shell 显式时限有界；原子编辑保留普通权限与脚本执行位。
- 正式 ProductHarness 使用 CodePilotApplication，实际原生 Git checkout 完整 commit；精确注入回归和未提交笔记；先独立验证初始失败，候选结束后在新环境注入隐藏材料验收。保护文件、空修复、Git 起点/暂存区/执行配置、模式与字节都检查；模型/Runtime/独立结果分别记录。
- 清单/环境/预算/隐藏材料/源码哈希冻结；独立 trial 和有限共享请求额度，任务所有最大额度不超 suite 上限。started/结果/报告原子保存，中断不重放，磁盘满保留旧报告。eval-compare 对版本/任务/模型/预算/环境漂移拒绝比较。
- 首轮冻结 6 次、96 请求上限，实际 83：5 个候选独立通过，最后一个验证器因本轮更新镜像标签导致旧索引丢失而不可评测；原分类又把 step_limit 的独立成功记为 unresolved。完整首轮报告保留，不覆盖、不混算。修正独立成功统计，增加唯一镜像引用租约与结束清理。
- 第二轮运行前重新冻结 6 次、每次最多 24 请求/24 步、总 144。官方 DeepSeek Flash Chat + Rootless Docker：六次独立通过，六次 Runtime completed，实际 62 请求；551,870 输入 / 16,240 输出 Token。Tomli 三次 CRLF/Decimal 跨模块修复，boltons 三次 slugify/分块回归；均为固定真实上游中注入的回归，不声称修复上游现存 bug。两轮合计 145 次，分组预算与失败全部保留；最终安装版/最新镜像另做 4 请求上限冒烟，实际 2 请求通过，本轮实际共 149（含重建后最终包的 2 请求复验）。
- 所有成功候选保护文件未变，资源为空、budget pending 为空，评测自有镜像别名释放。task_gate_passed=true，release_ready=false；B2 网络/秘密、硬磁盘配额、worktree+Docker Git、安全任务撤销仍明确阻断成熟发布。
- 当前离线 325 passed / 9 skipped；Rootless Docker 9 passed。Ruff 全目录/格式/mypy/wheel 通过。Python 3.11 干净 wheel 在仓库外 325 passed / 9 skipped，将未回收 transport 警告升为失败后通过；新增无密钥 CI，远程运行状态不以本机通过代替。
- 发现并补充真实 PTY 取消、非 TTY、存储失败、渲染失败、脚本执行位、镜像生命周期回归；安装测试发现旧 100ms 进程启动假设会误报，改成后代启动屏障后触发超时。另用输出背压稳定复现 3.11 暂停 PIPE 遗留，明确关闭已停止进程的 transport 后，两个版本均无该警告。
- 推荐阅读顺序及三题答案见 docs/learning/B5.md，掌握情况待确认。下一大阶段应回补 B2 执行与安全交付阻断，再用 B5 门槛复验；不另起 B6 或把未完成边界写成成熟发布。

## 本轮 B4 验收（2026-10-19）

- 统一 CodePilotApplication 装配，无第二套 Agent 循环。可执行扩展默认关闭；技能只注入元数据、正文按需加载；仓库不能启用 Hook/MCP/后台权限。
- Hook 固定 argv、before/after、严格 JSON、失败策略与独立审批；MCP 官方 SDK 1.30.0、受控后端 stdio、目录/Schema 快照复核、有界消息、断开未知结果不重试。MCP 每次调用新进程，不支持跨调用内存状态。
- 后台任务持久状态/日志游标、真实命令输出、精确取消和独立后端；`/tasks` 与 `tasks list/show/cancel/input` 接统一 CLI。PTY 双层不回显、EOF/窗口尺寸；前台退出取消任务，崩溃不复活，协调后标 unknown。
- explore/review 复用同一个 Runner/协议适配器，工具为父权限与固定读取集合交集；父子共用持久请求/Token/实际操作预算，子报告带来源且判定为 not_run。
- 修复两类实际调度死锁：控制请求不被写等待饿死；子代理不保留外层读租约，避免与父 Hook/后续读取成环。补真实进程、控制器崩溃、MCP 目录漂移/断开、Hook 超时、权限/配额/来源变化回归。
- 最终离线 305 passed / 8 skipped；8 项 Rootless Docker 独立运行通过。Ruff、格式、mypy、wheel 结果见 B4-checks；Python 3.11 干净 wheel 的 B4 全部 24 项测试通过，用户命令已按锁版本更新。不将 tmpfs 测试当掉电持久性证据。
- 官方 DeepSeek Flash + Docker 成功链：17 请求，两个子任务 succeeded，技能/Hook/MCP/后台测试实际调用，Runtime completed；控制器独立公开/隐藏 Unicode 测试通过、保护文件未变、后端关闭。一次额外 Git 查询错误也保留，不等于所有工具都成功。
- 失败保留：首次 9 请求发现嵌套读租约死锁，终止后 needs_reconciliation；第二次 15 请求，子报告输出截断且耗尽总请求额度，虽然独立修复测试通过，整体仍判失败。按用户高预算授权把本轮上限由 24 扩到 48，实际共 41 次，不隐瞒失败或重置计数。子输出改为可配置 8192，仍受父上限约束。
- B4 仅支持当前 run 拥有任务、无状态本地 stdio MCP、只读子代理。远程 MCP/OAuth、写 Agent、团队/合并、永久后台服务、运行中任意新目标编辑和完整 TUI 未实现/未验收。B2 遗留安全边界保持；下一大阶段 B5 必须把遗留阻断项纳入发布判定，不能直接宣布成熟发布。
- 学习说明见 `docs/learning/B4.md`，推荐阅读顺序及三道理解题答案已提供；掌握情况待用户确认。

## 本轮 B3 收尾验收（2026-10-19）

- 新入口 `sessions backup/migrate/restore/facts/diagnose` 与 `context ID [show|summary|reset]` / `/context` 已接统一产品服务；维护和查看零模型调用。
- 数据库 v2、事件 v1：升级前 0600 一致性备份，事务升级失败回滚；快照核验覆盖点、事件前缀及内容。恢复数据库不回滚代码，累计预算不倒退，恢复后必须协调。
- 三处真实控制器退出：模型调用前、Shell 副作用中、工具完成结果未入库。新进程阻断未知 run，活跃资源不误放，清理后协调补未知错误，独立核对无重复写入。
- 控制器事实带 run/call/sequence、退出码和工作区版本，外部编辑使旧结论 current=false；命令成功与独立需求验收分开。严格 JSON 摘要校验来源编号，仓库规则不提升为 system。
- 上下文可审查并撤回，撤回归档检查点、暂停自动压缩；强制 compact 才重新启用。已知官方组合能力/价格带日期，未知站点不套用；每次尝试按发送时费率保守估算，不声称费用硬封顶。
- 本轮离线 281 passed / 7 skipped，Rootless Docker 7 passed，Ruff/格式/mypy/wheel 通过。干净 Python 3.11 wheel 在仓库外验证 Local、数据库旧版本阻断/迁移/resume、Rootless Docker 与用户文件保留。
- 两协议各真实修复两个模块并通过公开/隐藏 Unicode 测试，严格压缩、新进程续问、撤回/备份通过；共 20 次请求尝试，包括最初事实消息位置触发的 2 次失败。成功链 64,179 输入 / 9,704 输出，失败 usage 未知；没有执行未完成的工具。详见 `docs/evidence/B3-finish-live*.json`。
- B3 在明确支持范围内通过受限验收，学习待用户确认。B2 边界、其他平台/模型、大窗口极值及总体产品发布门槛仍未通过。当时下一大阶段为 B4；最新 B4 结果见本页顶部，仍服从保留的权限和隔离边界。

## B2 首个增量：安全读操作与交互外壳

- `controlled-edit` 和 `ask-edits` 对无重定向、无网络和严格 Git 查询白名单的命令自动放行；`pwd`、`ls`、`git status/log/branch --show-current/rev-parse/ls-files` 及仅由 `&&` 连接的安全查询可直接执行。`read-only` 仍不因此获得进程能力。
- 管道、命令替换、未知命令、写 Git 子命令、`printf` 以及混入副作用的 `&&` 组合继续进入逐次审批；审计理由明确记录为 `safe_process_read` 或原有匹配规则。
- TTY 交互输入 `/` 时显示 `/help`、`/status`、`/model`、`/sessions`、`/exit` 菜单；未知斜杠命令只提示并可继续。非 TTY 仍使用稳定行输入，JSONL 不混入菜单控制序列。
- 交互提示符中误输入 `codepilot resume/run/sessions ...` 时直接提示先 `/exit` 再在 Shell 执行，不启动 Provider，避免把 CLI 命令当成自然语言任务并触发无关 Shell 探索。
- Action 审批现在显示实际 cwd、后端、资源和短指纹，Shell/秘密参数只显示脱敏摘要；执行前复核指纹。项目配置不能修改 Provider 安全字段、执行后端或扩大用户权限。
- 每个正式 run 保存私有工作区基线并在结束时记录路径级新增/修改/删除摘要，支持非 Git/脏目录事实审查；基线损坏时标记未知，不提供盲目撤销。

## B3 当前联合增量：长任务上下文与 Provider 可靠性

- 正式产品链现在支持 `protocol: responses` / `--protocol responses`，默认仍是 Chat Completions；消息、权限、工具、Trace、恢复共用同一 Runtime。官方 DeepSeek Flash 两种协议各完成真实 CSV 修复，初始测试失败、修改后测试成功，独立公开/隐藏验收均通过；用户笔记和测试文件保持原样。
- 每次 HTTP 尝试均在发送前以唯一 attempt_id 持久预留；摘要、主请求、重试共享额度。usage 按 ID 结算，缺失或崩溃保留 pending；新 run 和 reconcile 都不能清零。`sessions show` 展示实际计量及未知预留。老会话历史未计量用 `legacy_requests_without_usage` 标记，不能据此承诺费用硬封顶。
- 默认最多三次安全尝试，仅 429 明确拒绝或可证明未发送的建连失败进入可取消退避。认证、500、读写超时和消费流后失败不自动重放。SDK 自身重试仍为零；合法 Retry-After 超过等待上限时停止。
- 检查点 v2 绑定完整回合覆盖数、规范消息哈希及摘要校验和。跨进程优先复用，再以“旧摘要 + 新回合”滚动压缩；规范历史保留，摘要用 assistant 角色，无工具摘要器不会产生工作区副作用。`/compact` 与 `codepilot compact SESSION_ID` 只压缩，不发主请求、不启动工具。
- 100 回合确定性测试覆盖五次跨实例滚动压缩、用户目标修订及损坏拒绝；真实修复后的新 CLI 进程各完成一次摘要+续问，下一进程复用检查点各仅一次主请求。两个协议手动 compact 各一次摘要、0 主步骤、0 工具，工作区未变。
- 本轮真实共 23 次请求（两协议合计，含修复、resume、自动压缩、主动压缩），Rootless Docker 全部关闭；模型自述、工具结果、Runtime 与独立判定分别记录在 `docs/evidence/B3-live-repositories.json`。
- 本轮离线 265 passed / 7 skipped，Rootless Docker 7 passed；Ruff、格式、mypy、wheel 通过。完整命令结果见 `docs/evidence/B3-reliability-checks.txt`；设计 `docs/design/B3.md`，阅读顺序和三道问答 `docs/learning/B3.md`。
- 上轮未退出项已由本轮收尾区补齐；这一段为前次联合增量的历史证据。检查点 v1 仍不冒充 v2 来源覆盖，WorkingState 仍只是模型建议，独立控制器事实另行核验。
- 历史启动优化保留：SDK 按需加载，安装版启动退出约 0.21 秒；任务输出 65,536、请求/步骤 64、工具 256、期限 3600 秒。未实际发送 1M 上下文或生成 65K 输出。

## B1 本轮完成与验证

- 同一提示符任务共享持久历史，新增 `resume <id> [task]`、`sessions list/show/reconcile/delete/prune`，`run --session-id` 可连续任务。会话正文私有保存，默认 Trace/show 不输出正文。
- SQLite v1 追加事件与状态索引同事务提交；持锁重开重建索引，损坏事实/序号/未知 Schema 拒绝；独占内核写锁阻止同会话双写。
- Shell 与 Local argv 共用可取消进程生命周期，Docker worker 分块传输原始字节；有界预览、读搜、SHA-256、partial/quota_exceeded 独立于命令成功。输出、Context、历史、数据库及共享请求额度可配置。
- 取消清理根因已稳定复现并修复：gather 已传播取消，Runtime 二次 cancel 打断活动工具收尾。新增屏障回归及真实进程/容器取消验证。
- 重开核验工作区/dev/inode/模式/文件版本、历史配对和资源身份；未知副作用进入 needs_reconciliation，显式协调补“结果未知、禁止重放”错误，不伪造工具成功。过期及删除保护未交付 worktree/补丁。
- 离线全量：221 passed、7 个显式 Docker live skip；Ruff、格式检查、mypy、wheel 构建通过。根因屏障旧代码失败及开发中的容器握手截断失败均已定位，不用重跑成功覆盖失败记录。
- Rootless Docker：7 passed，含超过 100 MiB 输出、跨容器持久引用、配额停止和取消；运行后无受管容器。只操作用户 daemon，未修改公共 Docker。
- 真实 qwen3.5-flash + Rootless Docker：两次独立 CLI 进程、总 4 请求（预算 6）；Shell 输出 140,022 字节，resume 的 read_output 读取同 ID 中段标记，未重放 Shell。两个 Runtime completed；控制器独立核对 ID、字节数、工具读取与调用次数。输入 11,057、输出 667 Token。
- 超过 100 MiB 的容器样本：110,100,491 字节；worker 峰值 37,888 KiB、控制器 82,944 KiB，仅为受测样本，不作普遍性能承诺。
- 干净 Python 3.11 wheel 安装后在仓库外完成 Local run/resume 和 Rootless Docker；包确认从 site-packages 加载。
- 设计与学习：docs/design/B1.md、docs/learning/B1.md；证据 docs/evidence/B1-*。学习待用户确认，没有动手题。
- 未运行：Windows/macOS、其他 Python 版本矩阵、第二协议和真实磁盘硬配额；B2/B3 的网络/秘密治理、滚动摘要/复杂恢复仍待实现。原始输出可能包含敏感内容，保留在私有目录中，不能宣称任意秘密都可自动检测。

## 历史：B1 开发前的交付计划（已由上方实际证据更新）

- 用户要求一次开发一大阶段，后续按 B1—B5 交付；25 个技术细项只是内部验收索引，不逐项停顿。
- 下一轮完整完成 B1：取消清理修复、截断前输出及读搜/配额、基础持久存储、连续追问、列表/查看/重开会话，重开前核验工作区/资源并阻断未知副作用。详见 docs/design/B1.md。
- 基础持久化提前到 B1；当前仍未实现。B3 深化快照/迁移和完整崩溃协调，禁止未知写操作自动重放。
- 用户已授权 ROADMAP.md 正常修改、提交和推送；本轮顶部同步大阶段，原有历史内容完整保留。此前“不加入提交”的约束已撤销。
- B2 完善受控执行和工作区，B3 长任务/恢复/Provider，B4 扩展/后台协作，B5 真实任务评测与发布；联合验收贯穿各阶段。
- 本轮只做文档一致性验证，未重跑 Python、Docker 或真实模型，未修复取消问题。上轮测试结果仅保留历史证据，不能当成本轮或 B1 通过。

## 前次规划修订记录（已被上方大阶段交付约定更新）

- 用户本轮要求细化后续计划；已更新 `docs/PRODUCTIZATION_PLAN.md`，保留 P1/P2 证据和 P3—P8 顺序，拆为 25 个可验收增量，补上正常流程、重要故障、状态归属及退出门槛。
- 修正旧现状描述：正式 CLI、OpenAI-compatible 配置及原生 Shell Git 已存在；提示符尚不共享模型历史，恢复、完整 Action 和网络治理仍未实现。
- ROADMAP.md 当前存在重复续接规划与历史编号，本轮保持用户文件原样且不纳入提交；执行依据仍为产品化计划。
- 下一功能增量仍是 P3.1 截断前输出捕获；但本轮首次离线全量出现取消清理用例失败（200 passed、1 failed、4 skipped），单项复跑通过。必须先复现和修复该不稳定行为，不能因重跑通过就忽略。详见 `docs/evidence/PRODUCTIZATION_REVISION-checks.txt`。
- 本轮没有运行 Docker/真实模型或新增安全、恢复能力；规划设计与阅读问答见 `docs/design/PRODUCTIZATION_REVISION.md`、`docs/learning/PRODUCTIZATION_REVISION.md`。

## P2 / M11 当前证据

- 默认产品入口直接修改当前目录，不要求 Git 初始化或干净状态。已有未提交文件由用户继续拥有，产品启动和结束均不会清除它们；普通任务的结果立即位于源目录，无需手动应用补丁。
- Git 采用与 Claude Code 相同的 CLI 思路：status/diff/commit/branch/fetch/pull/push 全部通过 `shell` 使用原生 Git 参数体系，并经过既有 `process` 权限与审计。原有 `git_status`、`git_diff` 包装和 Docker 控制器特判已删除，避免维护第二套 Git 接口。确定性测试已在脏目录完成 init、分支、提交及本地裸仓库的 fetch/pull/push，并确认原有未跟踪文件保留。
- `--worktree` 是显式隔离入口，把 task ID 绑定到固定 commit 和状态目录中的 detached worktree。`require_clean_source` 默认关闭；打开后可要求源目录干净。
- `workspace.json` 以 0600 记录 UID、session/task/run、源仓库、任务目录、base commit、状态和补丁摘要；路径、所有权或当前修改不匹配时拒绝清理。
- 交付通过临时 index 纳入未跟踪文件，导出 Git 原生 `--binary --full-index` 补丁，覆盖新增、删除、文件模式和二进制内容；补丁为 0600 并带 SHA-256。
- 每份补丁都在同一 commit 的新 replay worktree 先检查、再应用；项目可配置固定验证 argv。验证副本完成后删除，有修改的 Agent 工作树和补丁保留，无修改任务自动清理。
- 两个显式隔离任务从同一 commit 创建 worktree，分别修改同一路径，结果互不覆盖、补丁摘要不同且源仓库保持干净。不存在的起点、启用严格检查后的脏源目录、重放冲突和带修改清理拒绝均有确定性测试。
- 早期真实 Qwen + Rootless Docker 隔离运行曾调用现已删除的 `git_status` 创建 `answer.py`；该历史证据只证明当时的 worktree 补丁链。当前 Git CLI 统一链另有 Local、Rootless Docker 和真实模型证据，不能用旧工具轨迹替代。
- 沙箱镜像现在安装 Git CLI。真实 Rootless Docker 4 项验收通过，其中新增场景在含用户文件的普通目录完成 init、commit、branch 并保留原文件；联网远端操作仍因 Docker 默认断网而未验证。
- 真实 Qwen 在默认直写 Docker 工作区完成 init、写文件、add、仓库级身份配置和 commit。首个 run 在提交后因八次 Provider 请求额度耗尽而未生成最终回答；第二个只读验证 run 完成，模型与控制器均确认 main 分支、干净状态和最新提交。证据见 `docs/evidence/P2-live-git.json`。
- 删除 Git 只读包装后做了三次兼容性实测：中转站 `qwen-turbo` 和 `qwen3-coder-plus` 都在首轮返回 `StreamContractError`、零工具调用，其中 Coder 的结构探针确认是无正文、工具分片、结束原因和 usage 的空流；DeepSeek 官方 `deepseek-chat` 成功并行调用两个 Shell Git 命令，2 步完成，准确报告 `master`、干净工作区和 `4f04ef3 initial baseline`，未修改文件。证据见 `docs/evidence/P2-native-git-model-comparison.json`。
- `uv run --locked pytest -q`：201 passed、4 个显式 Docker live skip；真实 Docker 套件 4 passed。设计、阅读路径和理解题答案位于 `docs/design/M11.md` 与 `docs/learning/M11.md`。

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
- 用户执行“初始化 git 仓库”时，首轮 Provider 在 0.91 秒后以 `APIConnectionError` 失败，工具调用数为零，因此与 Git 和 Docker 无关。此前终端只显示 `provider_error`；现在 `RunReport.detail` 保留脱敏异常类型，文本与 JSONL 结果都会展示该类型，连接/超时错误还给出重试建议。随后真实重试完成；控制器独立确认目录早已有 Git 仓库、`master` 工作区干净且最新提交为 `4f04ef3 initial baseline`。模型声称 `quick_sort.py` 未跟踪与真实状态不符，不作为证据。详情见 `docs/evidence/P1-provider-error-visibility.json`。
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
- 2026-10-15 早期真实 DeepSeek 复验曾因服务异常超时。服务恢复后执行一次受限 `deepseek-flash` 摘要：1 次请求，261 输入 Token、224 输出 Token；前缀、近期工具对、目标、约束、修改、测试和未验证风险检查全部通过。证据见 `docs/evidence/M10-live-success.json`。

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

- 宿主 bind mount 和 Context ArtifactStore 尚无经验证的磁盘硬配额；输出与产物容量限制不能替代磁盘配额。B1 已补基础持久会话与统一 CLI；完整 TUI、扩展和后续恢复深化未实现。
- 独立 Context store 默认仍可作为临时组件使用；正式产品由 B1 持久索引和私有文件保留 Artifact，跨进程可读，受保留期及显式清理管理。
- Token 估算不是任一模型 tokenizer 的精确值；Provider 现已计入消息、工具 Schema 和包装估算。已有跨进程滚动压缩真实样本，但没有大规模长任务语义质量统计。
- 未运行 Windows/macOS、Python 3.13、sdist 安装验证；真实 API 当前覆盖官方 DeepSeek Flash 的两协议读写、Shell 测试和压缩闭环；不推广到其他模型或网关。
- M01 的 wheel 安装证据只证明当时构建可安装与运行，不证明构建字节完全可复现；
  项目锁也没有完整固定隔离构建环境依赖。
- M02 Runtime 管理本次历史、跨消息 ID 配对、观察序号和终态；没有永久会话或恢复保证。
- Runner 预算为请求/工具尝试次数与总时限；DeepSeek 另有共享请求额度及单请求输出上限，没有账户费用硬上限。
  时限依赖协作式取消，不能强制中断阻塞代码。
- 终止时可留有无结果的调用；Runner 单次使用，不支持恢复重放。
- M02 本轮只构建 wheel，未重复独立安装；M01 的安装证据不能代替 M02 安装验证。
- 未验证 Windows/macOS 的进程组行为；POSIX 后代终止可能短暂留下僵尸记录；M04 旧演示轨迹在结束后写盘；M07 JSONL 逐事件追加，但不提供崩溃恢复。

当前边界：基础持久会话已由 B1 实现；完整网络/秘密 Action、长任务治理及整体产品发布门槛仍待 B2—B5。

下一阶段：回补 B2 的执行与安全交付阻断，再复验 B5 成熟发布门槛；不重做已有通过链。B2 的网络/秘密、worktree + Docker Git、存储硬边界与安全撤销缺口继续保留。

