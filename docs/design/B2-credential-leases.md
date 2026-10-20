# B2 私有 Git 凭据生命周期与发布边界

日期：2026-10-20（北京时间）。父提交 8dde7a7。机制与统一产品入口已实现，本机协议/确定性通过；最终 Rootless 联合验收受阻，release_ready=false。

本轮是完整的 GitHub App 仓库级认证产品链：可信用户配置/文件身份 → 原 shell 的显式 git_credentials 引用 → Action/审批/排队后复核 → 临时断网容器的本地 Git HTTP 网关 → 固定 GitHub API 签发短期令牌 → 精确仓库 Git smart HTTP → 撤销 → 持久结算/崩溃恢复/独立验收。没有逐 Git 子命令工具，没有令牌进入原生进程或模型环境。

## 契约和所有权

network.native.github_repositories 默认空，声明 name、client_id、installation_id、repository_id、owner/repository、private_key_file 和 contents=read|write。只支持 github.com / api.github.com 官方固定源，仓库引用与匿名 CONNECT 主机清单独立。native=true 仍受 Rootless/bind 限制；git_credentials 必须显式选择已配置名称，普通调用不签发令牌。审批展示引用及精确仓库读写范围，摘要包含私钥文件身份，不读取值。

原生 Git 使用原 HTTPS 仓库 URL；本次进程的安全 GIT_CONFIG 环境把获批仓库 URL 改写到容器回环网关。网关只开放 smart HTTP 的 info/refs、git-upload-pack 和按配置批准的 git-receive-pack，拒绝路径变体、重定向、调用者 Authorization/Cookie、非 Git 内容类型和超限正文。客户端头不透传秘密。TLS、DNS 固定 IP、公开地址与系统根沿已有控制器边界；普通 CONNECT 不得到任何令牌。

签发只请求一个 repository_id 和 contents 范围；JWT 使用 RS256，私钥来自工作区外的私有普通文件，不能复用 Provider 文件。响应须核验实际仓库、权限和过期时间。短期 installation token 只由控制器持有，不能把本地租期伪装成发行方过期。API 签发和撤销不自动重试；首次可能发送之前持久记录，未知结果保留。

已返回 token 以 AES-GCM 加密保存在状态根的独立凭据库；随机主密钥为 owner-only 文件，均不在工作区/会话正文/Trace/普通会话备份中。AAD 绑定租约与身份。正常退出/取消先撤销网关再请求服务端撤销；只有确认撤销才清理密文并结算。进程被杀后 resources prune 持会话锁、核验精确归属，使用密文恢复撤销，不重跑 Git。发行结果未知但未拿到 token 不能假装撤销成功，仍需要显式接受网络未知；无法解密的已知 token 不能静默删掉。

认证服务器可把秘密编码进任意业务数据；有限回显检测不能保证防御恶意服务端/任意编码。配置意味着信任固定发行方与仓库服务。Git binary pack/依赖脚本不等于可信代码。原生 LFS、SSH、子模块跨仓库认证、GitLab/PAT、私有包索引、bounded-copy 联网、远程 MCP 均不冒充本轮支持；支持矩阵和 B5 发布阻断同步更新。

## 验收

确定性 pytest、Ruff/format、mypy、非 editable wheel；真实 TLS 发行方验证 RS256、单仓库/权限、expiry、加密与请求头；实际 Rootless Git fetch/push 后独立远端 ref/正文验证；合成秘密不得出现在工作区、输出、Trace/会话正文；取消、超限、签发截断、权限扩大、私钥轮换、撤销失败、控制器 SIGKILL 后恢复、密钥丢失及审批拒绝都需独立断言。

先完成以上，再按固定版本/隐藏判据运行正常匿名联网修复与持久续问，官方 DeepSeek Flash 本轮跨全部任务总上限 16 次。四层结果分开。真实 GitHub App 安装未获专门凭据时不发送用户已有凭据、不新建外部 App/业务仓库，明确标 not_run；不借合成服务器把 release_ready 改为 true。

## 官方依据

- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
- https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
- https://docs.github.com/en/rest/apps/installations#revoke-an-installation-access-token
- https://git-scm.com/docs/http-protocol
- https://pyjwt.readthedocs.io/en/stable/usage.html
- https://cryptography.io/en/latest/hazmat/primitives/aead/

## 文件职责与目录原因

仍由 `app/service.py` 装配唯一 shell，`permissions/action.py` 绑定范围，`network/native.py` 拥有临时容器/代理共同清理。`models.py` 是可信声明与额度；`git_gateway.py` 解释有限 Git HTTP，不包装 git 子命令；`transport.py` 只负责一次固定公开 IP 的有限 HTTPS 交换；`credentials.py` 拥有发行/加密/撤销事实。会话层仅持不透明凭据 ID，维护入口显式调用恢复。私钥和令牌不进入 worker 镜像，固定桥仍由控制器启动，因此既有不可变镜像可以参与本轮组合验收。

状态根的 `.credential-leases/master.key` 和密文文件独立于 `SESSION/`。会话备份只有引用，不能靠复制备份获得凭据；只备份会话也不等于备份密钥库。缺失密钥或密文保持阻断。关闭租约移除 ciphertext，保留非秘密事实清单，不承诺 SSD 安全擦除、Python 内存即时清零或任意服务端编码的泄露检测。只确认服务端 204 撤销，401/超时不会猜成成功；若服务端无法确认，需要恢复发行方服务或专门核对，不能无条件强制 ready。

控制面每个引用至多一次签发和一次自动撤销，单次 15 秒/64KiB，单命令至多四个引用；不受耗尽的业务字节预算阻止清理。用户显式 prune 可再次请求撤销，但不自动重复 Git 或签发。业务请求共用连接/并发/总双向字节/租期；完整 pack 响应内存上限受 native.total_bytes 限制，不是无限流式大仓库代理。普通 CONNECT、只读子 Runner、MCP/Hook/后台没有获得认证能力。

## 产品使用入口

`examples/github-app.yaml` 提供默认关闭的可信用户配置示例。实际密钥必须是工作区外 owner-only、无链接的普通 RSA 文件，不可复用 Provider 文件；模型只能选择名字，不能提供 PEM 或 token。只在拥有专用 GitHub App/仓库授权时启用，普通业务命令不触发签发。

```text
codepilot --config /绝对路径/可信配置.yaml test --network --git-credential private_repo -- git fetch https://github.com/your-org/your-repo.git
/test --network --git-credential private_repo -- git fetch https://github.com/your-org/your-repo.git
```

上述零模型命令仍须正式审批，默认非 TTY 不自动允许。`--git-credential` 可重复选择至多四个名字。缺少 --network 或策略未开启在状态创建前拒绝；Provider fake 化前仍检查原 Provider 文件排除。Slash 和 CLI 同一 parser，输入错误只结束本条命令。


## 实际结果与待验证项

确定性与本机真实协议 26 项通过；全源码/仓库外 wheel 同为476 passed、40 skipped，静态与120 Python 源文件字节核对通过。Rootless 前期六项曾通过；最终两次串行组在主容器 create 60秒超时而失败，最后1 failed/26 deselected/62.07秒，不能标最终九项通过。新控制器 SIGKILL 与缺钥恢复由真实独立进程/发行方在普通 CI 验证，尚不能替代最终 Rootless 组合。

本轮发现既有 bind 主容器 create 前缺少持久归属；daemon 接受但客户端超时时，resources 列表无法定位随后出现的 Created 容器。此路径不是新凭据实现的成功证明，也未在本轮修复。已独立核验属于本轮临时目录的容器后发清理请求；请求超时与后来独立确认消失分别记录。下一整组先修精确创建恢复，再重验 Rootless/B5；禁止自动扩大旧清理范围或调整超时凑通过。

真实模型由于该 Rootless 阻断，单独使用 Local 可信宿主跑固定 Tomli 注入回归与跨进程只读续问，13/16请求、两次 completed、隐藏判据通过，源快照和保护文件不变；不算私有 Git 或 Rootless 模型联合验收。首次续问脚本配置扩展名错误在零请求时失败，核对原累计11请求后沿同一会话剩余额度继续，初始失败保留。官方 GitHub App 安装未运行，新 Docker 镜像未构建，release_ready=false。

配置采用保守命名范围：owner 允许 ASCII 字母大小写、数字和连字符；repository 以字母/数字/下划线开始，后续允许 ._-，拒绝双点及 .git 后缀。应按发行方 canonical full_name 填写大小写；不支持的变体在配置阶段拒绝，不能依赖 GitHub 的重定向来借用认证。
