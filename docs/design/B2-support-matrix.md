# B2/B5 发行支持矩阵

2026-10-20，北京时间。本文描述实际组合，不按功能名字推导其他组合。`release_ready=false`；私有 Git 官方安装与发行回归须分别有证据，不能拿合成发行方替代。

| 组合 | 当前行为 | 验收及限制 |
| --- | --- | --- |
| Linux Local，当前目录/非 Git/脏目录 | 直接工作；进程是可信宿主执行 | 不宣称强制断网、秘密隔离或磁盘硬配额 |
| Linux Rootless Docker，bind，默认 shell | 直接工作；network=none；不注入控制器凭据/代理 | 日常默认；bind 没有工作区磁盘硬配额，工作区已有文件含 .env 仍可见，环境白名单不是源文件秘密隔离 |
| Rootless bind，显式 shell network=true，匿名 Git/pip | 专有 network=none 容器、私有 CONNECT，精确域名 TCP443 | 匿名 GitLab、固定 wheel、真实 TLS Git 已有证据；域名授权不等于路径/方法授权；客户端负责 TLS |
| Rootless bind，GitHub App 仓库引用 | 同一 shell 显式 git_credentials；控制器代签发和认证，进程只见本地 Git URL | 本机 RSA/TLS/Git 与前期六项容器通过；最终九项联合组在主容器 create 超时而受阻；官方 GitHub App 安装尚 not_run，不能承诺最终组合/官方兼容已验收 |
| 请求级 HTTPS GET / fetch | 控制器按域名、路径和秘密引用发送；有限响应与重定向 | 已有 HTTP/真实模型证据；Bearer 引用不是自动签发或自动撤销 |
| Rootless bounded-copy | 断网 tmpfs 硬容量、检查点、版本条件交付/撤销、显式恢复 | 源 .git/.env* 不导入；网络配置与组合明确拒绝；tmpfs 易失，非掉电持久盘 |
| 显式 worktree + Docker | 固定提交的独立 clone，原生 Git 元数据在容器可见 | 不挂源 .git/凭据；联网临时容器沿实际执行副本，不自动扩成宿主 worktree 元数据访问 |
| 本地 stdio MCP / Hook / 后台 / 只读子 Runner | 既有有限权限与生命周期 | 不接入本轮网络 socket 和 GitHub 凭据；无跨进程的长期远程服务承诺 |
| rootful Docker / Local 的强制联网范围 | 拒绝使用受控原生代理能力 | 不能因为可运行 Git 就把它报告为本轮受控范围 |
| PAT、GitLab 私有认证、私有包索引、SSH、LFS、跨仓库子模块 | 本轮不提供凭据代理 | 已配置仓库范围不会隐式扩大；未配置来源不能取得 token |
| Windows/macOS、完整 TUI、远程 MCP、写 Agent/团队 | 尚未验收 | 保留后续能力；不进入当前发行通过范围 |

## 发行关口

仍保持 B1—B5 五个阶段，不另起 B6。凭据链完成后，下一整组是 B5 发行验收：冻结支持范围和版本；对默认 bind 与显式 bounded-copy 重跑固定上游修复、隐藏判据、保护用户改动与跨进程续问；复验取消、断流、磁盘/字节配额、网络未知、密钥丢失和精确资源清理；Python 3.11/3.14 非 editable wheel、旧会话升级、CI、成本/时延及残留核验。

官方 GitHub App 的单独条件：专门安装到测试仓库的 App、仓库 contents 权限、专用 owner-only RSA 私钥和明确允许测试仓库 push。未提供时不借用户 Provider key、Git 全局认证或宿主代理补跑，不新建外部 App/仓库；该格保持 not_run。真实账号成功后还须核验实际发行范围、服务端撤销后旧 token 被拒、远端 ref/正文以及会话/输出无秘密。

`task_gate_passed` 只代表某次固定任务的独立判据通过；`runtime completed`、工具成功、模型自述分别存储。只有发行范围内强制判据全部满足，才能另行修改 `release_ready`。历史 B5 重复评测不自动证明新增认证路径、任意仓库或一般任务成功率。
