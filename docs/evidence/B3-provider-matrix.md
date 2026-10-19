# B3 Provider 组合与费用边界

核对日期：2026-10-19 UTC。事实来源：[DeepSeek 官方规格与价格](https://api-docs.deepseek.com/quick_start/pricing/)、[Responses 文档](https://api-docs.deepseek.com/guides/responses_api/)，实际结果见 B3-finish-live*.json 与历史 B3-live-repositories.json。

| endpoint / model / protocol | 文本/工具/流/usage | 上下文与输出 | 本地或真实证据 |
| --- | --- | --- | --- |
| 官方 api.deepseek.com / deepseek-flash / Chat Completions | 真实观察通过；usage 缺失仍按 unknown 处理 | 配置窗口上限 1M；本地适配输出上限 65,536 | CSV 与跨文件 Unicode 修复、Shell 测试、压缩、resume；确定性半截流、超时、限流与取消 |
| 官方 api.deepseek.com / deepseek-flash / Responses | 同上，语义事件与最终调用清单需吻合 | 同上 | 同一产品链真实修复、严格摘要、resume；确定性终态/参数冲突零工具 |
| 官方 api.deepseek.com / deepseek-v4-pro / 两种协议 | 当前文档声明，不能视为本轮真实通过 | 本地适配保持较小输出上限 | 本轮未运行；已有旧摘要证据不升级为双协议修复通过 |
| 任意中转站 / 同名模型 / 任意协议 | unknown，用户配置明确选择后才尝试 | 使用配置估算，不能借用官方能力证明 | 不套用官方价格与成功率，SDK 错误类别可诊断 |
| 历史中转站 qwen-turbo / Chat Completions | 该组合 StreamContractError，零工具 | 本轮未运行 | 不概括为模型本身不支持工具 |
| 历史中转站 qwen3-coder-plus / Chat Completions | 该组合空流，缺正文/终态/usage | 本轮未运行 | 不概括为模型本身不支持工具 |

当前官方文档的服务输出上限高于本地适配器；本轮没有扩展适配器极值，亦未实际发送 1M 输入或生成 65K 输出。并行工具调度沿用内部资源约束，不能凭服务支持并行就放宽审批或写资源串行。

费率快照采用峰值、缓存未命中的保守估算：Flash 输入/输出每百万 Token 为 0.30/1.20 美元，Pro 为 1.32/3.96 美元；已核对官方页面，但价格可变化。每次尝试保存发送时费率，已知 usage 用十进制计量。缓存、时段与账单折扣没有混成“实际扣费”；未保存推理块也不能承诺推理历史完整兼容。

`known_cost_usd` 只是已计量请求的局部估算。pending、unpriced_attempts、legacy_requests_without_usage 明示未知，不能按零宣称账户费用硬封顶。恢复旧数据库保留较大计量及未知预留，可能更保守；它不抹除已发请求。API 网络故障注入采用真实 SDK 的受控传输，不冒充真实服务端出现过同样故障。
