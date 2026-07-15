# TIMELINE

单次连续的 Claude Code（多子 agent 编排）会话，2026-07-15，UTC 时间。人类（我）负责方向判断
和验收；AI（Claude Code 主控 + 子 agent）负责具体实现，见 `REPORT.md`"AI 工具坦白"。

```
07-15 02:00-02:20  读 CHALLENGE.md / KEEPA_QUICKSTART.md。
                    人类：明确任务范围。AI：无。

07-15 02:20-03:10  技术选型讨论——DB(SQLite vs Postgres)/后台任务(BackgroundTasks vs Celery)。
                    人类：否决"demo 小所以用 SQLite"的思路，要求按目标规模设计，定 Postgres+Celery。
                    AI：给选项对比，写进 ARCHITECTURE.md。

07-15 03:10-03:40  鉴权模型讨论——邮箱+密码注册登录，强制鉴权、不做匿名回退。
                    人类：拒绝我提议的"可选鉴权兼容题目示例"方案，要求强制且不hedge。
                    AI：设计 users/auth_tokens schema，画时序图。

07-15 03:40-04:30  会话记忆架构——短期(session)/长期(user偏好)分层，调研 LangGraph
                    checkpointer/store，发现工具调用记录会丢的设计漏洞并纠正。
                    人类：指出工具调用要包装成真正的 tool call，否则消息记录丢失。
                    AI：调研 LangGraph 文档，重设计 ER 图和 /chat 时序图。

07-15 04:30-05:00  HARNESS.md（目标/验收标准/证据）+ 前端质量红线（禁止 AI 味的黑名单、
                    WS 流式渲染的坑）+ 项目目录结构定稿。
                    人类：定"禁止 AI 味"的具体黑名单、点出流式渲染的常见坑。
                    AI：写 HARNESS.md 全文。

07-15 05:00-06:20  Phase 1-3：后端骨架(models/config/docker) -> 鉴权 + Keepa 客户端/
                    eligibility 规则(并行) -> ETL/upc/eligibility 端点 -> Celery/refresh/
                    每日定时刷新。每阶段子 agent 实现，我独立复核(真实 docker compose up、
                    真实 curl、真实 psql 查询)，不只信 agent 自述。
                    人类：验收，发现并处理 Keepa 网络不通的问题，决定用合成数据继续开发。
                    AI：实现 + 自测；我独立复核 + 写验证证据到 test_evidence/。

07-15 06:20-07:40  Phase 4：LangGraph agent(6个工具/checkpointer/store) + /ask + /chat +
                    WS 流式端点。发现并修复 /ask 的 prompt 问题(见 REPORT.md 的 prompt 迭代)。
                    人类：定 LLM 供应商(DeepSeek)，提醒 WS 流式需求，验收真实多轮对话。
                    AI：实现 + 修 2 个真实 prompt bug；我用真实 curl 跑通场景 A/B，查
                    checkpoint 表确认工具调用持久化。

07-15 07:40-08:20  Phase 5-6：Vue 前端(骨架+auth+数据页 -> chat 流式 UI 两波) + 验收脚本
                    (verify_auth/upc/chat/refresh_resume/cost_report/verify_all)。
                    发现并修复 refresh 断点续跑的计数越界 bug(done+failed 超过 total)。
                    人类：验收 chrome-devtools 截图，处理并行 agent 之间的 docker 容器争用。
                    AI：实现 + 真实 kill/restart 测试；我独立复核截图和 pytest。

07-15 08:20-08:50  发现设计风格与目标公司(Supersonic Supply)品牌不符，重做前端视觉
                    (白底/藏青/电光蓝/几何无衬线，替换原先的复古清单风)，只改样式不动逻辑。
                    人类：给出公司官网参考，要求对齐品牌。AI：restyle + 截图复核。

07-15 08:50-09:20  收尾：README.md/REPORT.md/TIMELINE.md 撰写，成本核算脚本产出核对。
                    人类：审阅交付物内容是否如实。AI：起草三份文档。
```
