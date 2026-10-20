# B2 原生命令受控联网

日期：2026-10-20。状态：明确支持范围验收通过；父提交 de27c31，release_ready=false。完整交付范围为用户配置、原 shell 工具显式 network 开关、Action/审批、单调用隔离容器、Unix socket CONNECT 代理、累计额度/租期、原生 Git 与固定依赖、取消/未知结果/崩溃资源管理、CLI/诊断/文档和联合验收。不增加 Git 子命令包装工具。

## 契约与状态所有权

`network.native` 默认关闭，明确声明精确域名；协议能力为 CONNECT 承载的 TCP:443，不承诺窥探加密后的 URL/方法或代替客户端 TLS 校验，不能继承 HTTP 请求代理的路径/凭据范围。仅 Linux Rootless Docker bind 工作区支持；Local、rootful、bounded-copy 明确拒绝。主容器与扩展继续断网且不挂载代理。

同一个 shell 增加 `network: bool=false`，仅 native 模式启用时展示；true 必须 process 审批，安全读白名单不能放行。批准绑定命令摘要、策略、目的地、工作区/后端身份，排队后再复核。控制器消费开关后调用原命令，不生成 Git/pip wrapper。每个联网调用启动专有临时断网容器，看到同一 bind 工作区，任务结束删除整容器；只有它只读挂载随机、owner-only 目录内单个代理 socket。容器回环桥把原生 HTTP CONNECT 转到 socket，代理核验主机、443、公开单播 DNS/IP 后连接固定地址；关闭代理、取消或控制器崩溃都不能回落直连。桥只转发字节，无业务密钥。

累计连接/并发/双向字节/租期由主 run 唯一账本维护；不得因新建临时容器重置。发送前持久记录 native 租约和网络 pending，正常命令完成且连接已结束才可结算；超时、取消、代理中断或未关闭连接保留未知状态，沿 network_pending / --accept-network-unknown，不自动重放。工具成功不等于 Git 远端或测试独立通过。

私有租约清单记录 controller PID/start、容器随机名/backend_id/镜像/返回 ID、socket 与父目录 inode、生命周期和统计。create 前就登记，不能只依赖返回 ID。崩溃后 resources prune 默认预览，核对 owner/控制器已结束/精确容器标签与 socket 身份才能清理；creating 结果未知不假装不存在。会话 reconcile 不跳过活容器或不确定清理。

秘密仍通过既有 HTTPS 请求代理按域名/路径引用使用，不注入任意 CLI 环境，也不为 CONNECT 自动附加秘密。依赖脚本属于已批准 process，可写工作区；域名允许不意味着供应链内容可信，锁版本/哈希与控制器独立验收分别记录。默认目录仍直接工作；Git metadata、已有用户差异按现有边界保留。native 不提供任意 Shell 变化自动撤销资格。

## 验收与重要故障

先 `.venv/bin/python -m pytest -q`、Ruff、format、mypy 和 wheel；再 CODEPILOT_DOCKER_LIVE=1 实测 Rootless：原生 Git ls-remote/fetch 到固定公开 GitLab 仓库、临时受控 HTTP Git 服务的 fetch/push、固定纯 Python wheel 按哈希安装并导入。TLS 受控服务仅测试替换 DNS 与信任根，生产没有私网开关。最后官方 DeepSeek Flash 至多 12 请求（单阶段总额），完成联网查询→文件产出/测试；不触发业务远端写入，四层事实分别保存。

重要失败至少覆盖：①网络=false/直连/代理绕过/未授权域名与私网答案拒绝；②解析/连接/隧道超时、并发与字节/租期耗尽后真实断连；③父取消/后台子进程/代理退出/控制器被杀后不再联网，租约与容器可识别；④审批拒绝/过期和 queued 取消零联网；⑤容器创建/桥启动/清理失败无假 closed，无隐式宿主回退；⑥未知联网结果阻断续问与误清理，普通 accept-workspace 不能解除。未验证项和供应链风险不得用 mock 成功替代。

官方依据：Docker none 驱动仅回环 https://docs.docker.com/engine/network/drivers/none/；Git http.proxy https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpproxy；pip 代理 https://pip.pypa.io/en/stable/user_guide/#using-a-proxy-server 。


## 实际结果与限制

最终命令、版本、计数、失败及产物哈希见 ../evidence/B2-native-network-checks.json。源码离线 450 passed/31 skipped；Rootless 联合 82 passed，最终原生 11 passed；官方 DeepSeek Flash 在 12 请求总上限内实际 10 请求/11 成功工具，独立断网验证两个不同仓库与非法目录通过，保护字节未变。模型自述/工具结果/Runtime/独立判据原样分别保存到 B2-native-model.json，生成脚本字节保存到 B2-native-candidate.txt。Python 3.11 仓库外 wheel 的最终计数以 checks 文件为准。

GitHub 的直连在宿主也失败，不能静默继承代理环境绕过范围；匿名 Git 联合验收改为固定公开 GitLab 仓库，失败事实保留。真实 fetch/push 使用独立 TLS/git-http-backend 测试服务，测试仅替换 DNS/端口路由和客户端 CA，不给生产增加私网绕过选项；外部业务仓库未执行 push。依赖验收为 idna 3.20 单个纯 Python wheel 的锁定哈希安装与独立离线导入，不推广为所有构建脚本/平台 wheel 已兼容。

本次复核修正：租期不能从模型等待前计时；下载空闲按双向共同活动；项目关闭总开关同时关闭继承 native；socket 监听前登记资源；child 清理失败不能被主容器关闭掩盖；桥/清理故障转换为固定工具错误；/proc 退出竞态属于已结束而非清理失败，Python 3.11 的真实测试已覆盖。

### 目录原因

- app/service.py 与 ui/cli.py：沿唯一正式产品入口装配，不新建第二套 Runner。
- network/models.py：可信配置和额度；native.py：单调用生命周期；egress.py：唯一实际出口；bridge.py：容器固定字节桥；leases.py：精确身份与维护。各模块拥有不同资源，不增加通用服务或空抽象。
- workspace/docker.py：原容器能力增加单个只读 socket 挂载、固定桥启动和 create 前回调，不理解 Git/pip 子命令。
- permissions：批准参数、作用域与固定环境名；sessions：沿原事务事件管理未知结果、资源与 ready 阻断。
- tests：实际 socket、真实 Rootless、正式 CLI 与付费模型分开；scripts/verify_b2_native_live.py 使用主产品并在新的断网容器做独立验收。
- docs/design、learning、evidence：分别保存契约、阅读理解与可追溯事实，不把文档或模型自述当产品完成。

现有 codepilot 可执行文件是本源码的 editable 安装，实际 TTY test --network 已独立执行通过；新 wheel 为非 editable 安装验证。worker 镜像保持既有不可变 ID，固定桥由控制器冻结源码后传入 child，不能把组合通过写成新镜像构建通过。成熟发布和私有认证仍未通过。


### 宿主解析器程序的工作区边界

解析器进程不能沿用运行时可变的 sys.executable 软链接：当 .venv 位于 bind 工作区时，容器可能替换它，导致下一次宿主辅助进程执行工作区代码。模块在启动时 resolve 固定实际解释器，使用 -I -S 排除工作区/PYTHONPATH 和 site/.pth；真实解释器必须位于工作区外。应用在读取 Provider 凭据和创建状态/容器/Trace 前预检，HTTP/原生工具独立构造同样核验，doctor 将无效联网配置标为未就绪。

测试实际提供可写标记的替身解释器，替换 sys.executable 后 marker 仍不存在；真实解释器落入工作区时原生与 HTTP 均拒绝且状态目录未建立。最终源码/非 editable wheel 450 passed/31 skipped、相关 Rootless 65 passed。未增加模型调用；此前模型十次请求的证据保持版本归属，不能声称它重新验过此补丁。
