# 从现有核心到可用产品：路线阅读记录

> 历史记录：本次后续决策已改为 B1—B5 大阶段交付，基础持久化提前到 B1，ROADMAP 已允许提交。下面的“只做 P3.1”和逐项交付等表述仅描述前次计划；当前以 PRODUCTIZATION_PLAN 第 2 节和 docs/design/B1.md 为准。

本轮只修改规划；实现和掌握程度均不能由文档完成推断。本记录不含动手题。

## 推荐阅读顺序

1. `docs/STATUS.md`：确认当前证据和 P3 待实施，避免按功能名称误判完成。
2. `docs/PRODUCTIZATION_PLAN.md` 第 1—3 节：看现状、阶段依赖、状态所有者；再看 P3.1，确定下一代码增量。
3. `src/codepilot/app/service.py` 的 `CodePilotApplication.run`：看唯一产品装配链及资源关闭，理解新能力为何必须进入这里。
4. `src/codepilot/tools/shell.py` 的 `_read_limited`、`ShellTool.run`，以及 `context/manager.py`：前者先裁剪，后者只能保存收到的结果，所以不能从旧 Artifact 找回已经丢失的中段。
5. `src/codepilot/config.py`、`permissions/executor.py`：理解现有配置合并与审批，再读 P3.2 的信任上限和完整 Action 要求。
6. 产品计划 P4/P5/P6：先让历史有界，再使会话持久且可恢复，最后完善协议及预算治理。
7. P7/P8 与 `docs/design/PRODUCTIZATION_REVISION.md`：扩展治理、真实任务验收和基础版交付界限。

## 一次产品任务应该经过什么

用户在脏目录提问 → 当前配置和信任上限校验 → 会话/任务/run 归属 → 请求预算与上下文视图 → 模型完整调用 → Action 审批 → 调度 → 后端副作用与原始输出 → 记录真实文件/测试事实 → 继续模型循环 → 唯一 Runtime 终态 → 独立验证 → 展示修改、未验证项和资源 → 下一条用户消息继续。

当前代码已覆盖其中不少环节；共享历史、完整 Action、持久会话及恢复仍是计划。阅读轨迹不能被当作全部已实现。

## 三道理解题及答案

### 1. 为什么 P3.1 必须在执行端保存输出，不能只加强 ContextManager？

Shell 的 `_read_limited` 现在排空管道但只保留前若干字节。ContextManager 运行在工具返回之后，此时中段已经丢失。应在截断之前分块保存原始字节，再生成有界模型视图；Docker 的 worker 传输也必须遵守此契约。只让 Context 写更大文件不能修复上游损失。

### 2. 为什么会话恢复不能直接加载默认 JSONL Trace 并重新执行最后一个工具？

默认 Trace 为隐私省略正文，不能重建完整对话；最后一个未记录结果的工具也可能已经写入文件或完成 commit。恢复需要受控正文存储、持久事件覆盖点、资源归属和副作用协调。不确定时进入 needs_reconciliation，重新执行可能重复或破坏用户修改。

### 3. 如何判断一个“运行成功”的 Agent 真正完成任务？

先分开四层：模型说完成、工具调用成功、Runtime 正常终止、独立任务判据通过。前面三项都不能推出第四项。验收还需核对目标文件/测试、用户原差异、预算与残留资源；验证器未运行就写 not_run，不能用 completed 替代。真实任务重复结果用于衡量可用性，fake 只证明控制流及故障边界。
