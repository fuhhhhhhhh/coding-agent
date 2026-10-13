# 开发状态

当前里程碑：M02（本轮到此结束）

最近通过：M02 非流式单 Agent 循环；M01 已通过

实现状态：通过离线验收

学习状态：用户报告已阅读 M01 核心代码；M02 阅读说明已交付，未验证独立掌握程度。

学习偏好：用户于 2026-10-13 明确要求不再留动手题，由 AI 直接完成修改。
原 M01 命名修改已由 AI 实施并验证，不记作用户独立贡献。

日期：2026-10-13（Asia/Shanghai）

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

- M03 及以后未实现；fixture 执行入口不是通用工具系统。没有通用 Shell、注册/schema、
  权限、流式组装、会话持久化或 CLI 框架。
- 未运行 Windows/macOS、Python 3.13、sdist 安装验证或真实 API；真实协议兼容需要后续真实环境验证。
- M01 的 wheel 安装证据只证明当时构建可安装与运行，不证明构建字节完全可复现；
  项目锁也没有完整固定隔离构建环境依赖。
- M02 Runtime 管理本次历史、跨消息 ID 配对、观察序号和终态；没有永久会话或恢复保证。
- 预算为请求/工具尝试次数与总时限，没有真实 token 或费用限制。
  时限依赖协作式取消，不能强制中断阻塞代码。
- 终止时可留有无结果的调用；Runner 单次使用，不支持恢复重放。
- M02 本轮只构建 wheel，未重复独立安装；M01 的安装证据不能代替 M02 安装验证。
- 未验证通用子进程树管理；轨迹在演示结束后写盘，不提供崩溃恢复。

当前阻碍：无。

下一增量：用户明确推进后进入 M03；先建立由 Pydantic 参数模型驱动的 ToolSpec、工具目录和
统一执行入口，再分别交付工作区文件工具、受限搜索与 shell 基础行为。M04 负责原子修改、
外部变更冲突和完整进程树生命周期，不把这些保证含糊地留在两个阶段之间。

