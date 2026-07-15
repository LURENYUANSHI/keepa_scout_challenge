# TIMELINE

单次连续的 Claude Code（多子 agent 编排）会话，2026-07-15，**UTC+8**（本机本地时区）。时间点
锚定方式：`candidate_package/test_evidence/*/` 里每份证据文件的真实写入时间戳（未被后续操作
覆盖，比数据库里的 `snapshot_at` 可靠——`asins.snapshot_at` 是 upsert 语义，后面跑真实 ETL
会覆盖掉早期合成数据的时间戳，之前那版 TIMELINE.md 就是被这个坑绕进去了，重新核对时发现的）。
人类（我）负责方向判断和验收；AI（Claude Code 主控 + 子 agent）负责具体实现，见
`REPORT.md`"AI 工具坦白"。

```
07-15 11:55-12:00  会话开始，先处理了一个不相关的前置请求（迁移 .claude 工作区配置到本项目），
                    不计入本挑战工时。
                    人类：提出迁移请求。AI：执行迁移。

07-15 12:00-13:21  读 CHALLENGE.md/KEEPA_QUICKSTART.md。技术选型讨论——DB(Postgres)/后台任务
                    (Celery)/强制鉴权/会话记忆架构，调研 LangGraph checkpointer/store 并发现
                    工具调用记录会丢的设计漏洞。写 ARCHITECTURE.md + HARNESS.md（两份文档
                    13:21 写完）。
                    人类：否决"demo 小用 SQLite"的思路；要求强制鉴权不做 hedge；指出工具调用
                    要包装成真正的 tool call；定"禁止 AI 味"的前端黑名单。
                    AI：给选项对比、调研 LangGraph 文档、写设计文档全文。

07-15 13:21-13:31  项目结构定稿(ARCHITECTURE.md §5)，准备拆解成可并行的实现阶段。
                    人类：确认可以开始实现。AI：拆任务、准备子 agent 简报。

07-15 13:31-13:41  Phase 1(后端骨架) -> Phase 2a(鉴权)+2b(Keepa客户端/eligibility规则，并行)。
                    人类：独立复核（真实 docker compose up、真实 curl、真实 psql 查询），不
                    只信 agent 自述。
                    AI：三个子 agent 实现 + 自测。

07-15 13:41-14:06  Phase 3a：ETL + /upc + /eligibility 端点。发现开发环境连不上 Keepa
                    （DNS 解析到网络测试哨兵地址），决定灌合成 fixture 数据继续开发。
                    人类：验收，处理 Keepa 网络问题，决定用合成数据的方案。
                    AI：实现 + 自测；我独立复核 + 写验证证据。

07-15 14:06-14:30  Phase 3b：Celery + /refresh 断点续跑 + 每日定时刷新。发现并修复计数越界
                    bug（done+failed 曾超过 total）。
                    人类：验收真实 kill/restart 场景。
                    AI：实现 + 真实 kill/restart 测试 + 修 bug。

07-15 14:30-15:38  Phase 4：LangGraph agent(6个工具/checkpointer/store) + /ask + /chat +
                    WS 流式端点。发现并修复 2 个真实 prompt bug（/ask 对具体 ASIN 解释类问题
                    和主观推荐类问题错误地跳过 SQL 查询）。
                    人类：定 LLM 供应商(DeepSeek)，提醒 WS 流式需求，用真实 curl 验收多轮场景，
                    读了失败 case 后亲自改了两版 prompt。
                    AI：实现 + 自测；我查 checkpoint 表确认工具调用持久化。

07-15 15:38-16:43  Phase 5(前端骨架+auth+数据页 -> chat流式UI两波 -> 对齐公司品牌重做视觉) +
                    Phase 6(验收脚本，含真实 docker compose kill/restart)，两个并行推进。
                    并行 agent 之间发生过 docker 容器争用（一个 agent 遗留的后台进程在反复重启
                    api/worker，跟我自己的手动验证撞车），排查后确认不是代码 bug。
                    人类：验收 chrome-devtools 截图，给出公司官网做视觉参考，排查容器争用。
                    AI：实现 + 真实浏览器测试 + 真实 kill/restart 测试。

07-15 16:43-17:11  收尾：README.md/REPORT.md/TIMELINE.md 撰写，git 分批提交(9次，按功能模块)
                    并 fork 推送。开发环境网络意外恢复，补跑真实 python -m app.etl(32/32成功，
                    消耗97个Keepa token)、真实 POST /refresh(32/32成功)、真实浏览器端到端走查
                    (含真实价格异常检测)，更新文档里的真实数字。
                    人类：确认提交方式(fork推送)，验证网络诊断细节。
                    AI：起草文档、提交、真实数据验证。

07-15 17:11-       进行中：发现 /chat 的最终回答文本不是真正的 token 级流式输出（工具调用是
                    增量推送的，但答案文字是生成完一次性推送），修复中；随后计划把前端组件库
                    换成 PrimeVue。
                    人类：实测发现流式问题，定优先级(先修流式再换UI库)，选定 PrimeVue。
                    AI：诊断 + 修复中。
```
