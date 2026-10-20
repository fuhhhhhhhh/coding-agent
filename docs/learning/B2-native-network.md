# 原生命令受控联网：阅读记录

本轮核心思路：保留一个简单的 shell，把联网当成每次命令明确申请的能力。默认容器没有外网；批准联网后，控制器启动一个临时容器，只给它挂载一个私有代理入口。Git、pip 仍是原生程序，控制器检查域名、IP、次数、字节和时间。命令结束先收回网络，再删除整个临时容器；失败和崩溃留下可核对的事实，避免误以为远端没有执行。

## 推荐阅读顺序

1. src/codepilot/app/service.py::_network_tools、ui/cli.py::build_parser 与 operations.test_command：先看正式模型与零模型入口如何复用同一 shell、权限、调度和会话。
2. network/models.py::NativeNetworkConfig、permissions/action.py::build_action：理解独立的精确 TCP:443 授权、审批指纹、默认关闭与环境名声明。
3. network/native.py::NativeShellTool.run：跟踪普通调用、临时容器、固定回环桥、共同清理与未知结算。这里是一次执行的生命周期所有者。
4. network/egress.py::EgressBudget/ConnectProxy、broker.py::resolve_public：看转发前扣额、固定数字 IP、实际流关闭及可终止解析。
5. network/leases.py::recover_lease、sessions/store.py::finish/native_leases/reconcile：看为什么主容器关闭不等于全部资源关闭，以及崩溃后为何不能直接续跑。
6. tests/test_native_network.py、tests/test_native_docker_live.py，最后 docs/evidence/B2-native-network-checks.json 与 B2-native-model.json：分别核对确定性机制、真实容器/原生 Git/pip/故障和真实模型的四层结论。

## 一次正常轨迹

用户明确请求查询远端版本 → 模型调用 shell(network=true) → Action 展示域名和 process 副作用 → 用户批准 → 排队后复核 → 首次申请启动 run 级租期 → 创建 owner-only Unix socket → 私有清单和资源引用在 Docker create 前持久化 → 创建同一 bind 工作区的专有 network=none 容器 → 固定回环桥 → Git 发 CONNECT → 核验目标和整组公开 DNS → 连接之前持久标记未知 → 固定 IP 转发并计数 → 原生客户端校验证书并获取 Git 数据 → 命令零退出且无代理错误 → 关闭代理和整容器 → 保存 closed → 才清除本命令的 network_pending。独立 Git/文件验收另行给出通过结论。

## 故障轨迹

- 未授权域名/端口/私网 DNS：拒绝，零外连；不会自动改用宿主网络。
- 真正发出后超限/超时/取消：断开双向流并删除容器，保留网络未知。接受工作区不代表接受远端未知。
- 桥启动失败：删除已经创建的容器；返回脱敏工具错误，不把异常正文给模型。
- child 删除失败：socket 先撤销，清单 cleanup_failed；主容器即使关闭也不能让会话变 ready。正式 prune 精确核验后回收。
- SIGKILL 控制器：代理进程随之消失，容器无直接外网；新控制器持会话锁核验标签/镜像/ID/只读根/能力与 socket 身份再清理，不重放旧 shell。
- create 返回 ID 前结果未知：随机名称已记录，但一次 inspect 找不到不证明 daemon 没接受；保持保守阻断。

## 三道理解题与答案

1. **为什么不直接给主容器开启网络？**  
   那会同时给后台进程、扩展和后续未批准命令联网能力。专有容器只挂载本次 socket，退出整容器能回收后代，普通链路仍断网。

2. **为什么 HTTP 的路径授权不能直接用于 Git CONNECT？**  
   CONNECT 中的 TLS 字节是加密的，代理只知道域名与 TCP 端口，无法证明里面的 URL、方法或内容。必须独立配置更宽的域名权限，不能假装继承原 HTTP 路径/秘密限制。

3. **为什么命令失败、资源清理成功后还可能要求协调？**  
   失败可能发生在远端已接受请求之后，清理只能证明本地不再执行，不能证明远端未变。network_pending 保留此事实，显式接受未知也不自动重放。

## 目录与设计原因

network/ 负责授权配置、控制器出口、固定桥和租约；workspace/docker.py 只负责真实容器，未增加 Git/pip 工具；permissions/ 负责批准内容与复核；sessions/ 沿原 SQLite 事件管理未知状态；app/ 和 ui/ 仅装配和分发。测试区分真实 socket、实际 Rootless 与付费 Provider，证据不混合计数。实现理解尚未由用户确认，不记录为用户已掌握。
