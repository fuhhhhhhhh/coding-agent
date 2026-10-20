# 仓库凭据为什么留在控制器

本轮要解决的是：Coding Agent 可以用 Git 操作一个私有仓库，但不能因此拿到可复制到其他进程或其他仓库的 token。用户提供的是仓库引用，控制器向 GitHub App 发行接口申请该仓库的短期令牌。原生 Git 仍执行普通命令，只在这次进程里把指定 HTTPS URL 改到本地网关。网关核验操作后向真实 GitHub 发认证请求，最后撤销 token。取消和崩溃都不能直接把会话标成干净。

## 推荐阅读顺序

1. `src/codepilot/app/service.py:CodePilotApplication._network_tools`：先看正式产品如何把联网版接回唯一 shell，以及 Provider 凭据文件为什么只按路径传来做排除。
2. `src/codepilot/network/models.py:GitHubRepository`、`src/codepilot/network/native.py:NetworkShellArguments`、`src/codepilot/permissions/action.py:build_action`：理解可信配置、模型可选引用、仓库读写范围和审批指纹分别归谁负责。
3. `src/codepilot/network/git_gateway.py:git_environment/resolve/handle`：跟踪原生 Git URL 如何重写、怎样保留 hooks/fsmonitor 安全配置，以及哪些请求在签发前被拒绝。
4. `src/codepilot/network/credentials.py:GitHubLease.acquire/CredentialVault.encrypt/revoke`、`network/transport.py:exchange`：看 TLS 之后读私钥、单仓库签发、先加密后使用、确认撤销后结算。
5. `src/codepilot/network/native.py:NativeShellTool.run` 的 cleanup、`sessions/store.py:finish/reconcile/credential_leases`、`app/operations.py:prune`：看为什么主容器关闭还不足以恢复 ready，以及另一个控制器怎样清理旧凭据。
6. `tests/test_git_credentials.py`、`tests/fixtures/credential_controller.py`、本轮 `docs/evidence/B2-credential-*`：按正常 Git push/fetch→拒绝/配额/取消→SIGKILL→正式 prune 阅读独立断言；最后读 `docs/design/B2-support-matrix.md`，区分真实协议测试与官方账号验收。

## 一次正常执行轨迹

用户先在可信配置声明 `fixture` 是一个具体仓库的 `contents=write` 引用。模型发 `shell(command='git push https://github.com/owner/repo.git main', network=true, git_credentials=['fixture'])`。Action 摘要包含仓库、读写权限、引用和私钥文件身份；审批及排队后复核通过，才进入专有容器。

Git 的 URL 改写只存在于本次进程环境，不写 `.git/config`；token 不在这些变量中。本地网关收到 receive-pack 广告请求，先核验路径、方法和头，再触发按需签发。发行方 TLS 可信后读 PEM，签 RS256 JWT，发送前保存 issuing。返回 token 后 AES-GCM 密文先落盘，核验仓库/权限/到期，再发 Git Basic 认证请求。响应完整收集且没有常见秘密回显后才交给 Git。命令结束先停网关，再独立请求服务端撤销，最后关闭容器并结算；只有这些事实确定成功，才恢复 ready。

## 重要失败如何阅读

- 权限/路径/头不合规：本地拒绝，零签发，不能把这类拒绝记成远端结果未知。
- 签发发送后断流：没有收到 token，不能针对它撤销。保留 issuing，不自动重试。清理容器后须显式接受网络未知；记录的是人为接受，不是“已撤销”。
- 已收到 token 但服务端撤销失败：密文保留，普通 reconcile 不能跳过。`resources prune` 使用独立有限控制面额度撤销，不重跑 Git。
- 控制器 SIGKILL：另一进程取得会话锁，从密文和精确归属恢复撤销、清理原容器/socket；旧工具结果仍未知，须显式协调。
- 主密钥丢失/密文移接/私钥轮换：失败关闭并保留证据。不能生成新主密钥假装旧 token 不存在，也不能拿新私钥替代已审批版本。

## 三道理解题及答案

1. 为什么不把短期 token 放进 `GITHUB_TOKEN` 就结束？
   答：进程得到原文后可以读取、复制或传给其他请求；短有效期不等于仓库操作边界。这里进程只能调用精确仓库的 Git 网关，token 留在控制器，使用范围由发行方和网关共同约束。
2. 为什么有 AES-GCM 密文，还必须保存 issuing 阶段？
   答：服务端可能已签发，但响应尚未到达控制器，此时没有可加密的 token。先保存意图才能在崩溃后保留这个未知窗口；加密不能提供跨外部 HTTP 和本地数据库的原子事务。
3. 为什么工具返回成功、Runtime completed 后仍可能 needs_reconciliation？
   答：命令退出只证明本地 Git 的结论，不能证明撤销、临时容器回收和持久结算都成功。任何凭据资源未关闭或网络结果未知，都必须单独处理；模型说“完成”不能代替这些事实。

实现与证据状态以 STATUS 为准；学习说明已交付，未验证用户独立掌握。无动手题。
