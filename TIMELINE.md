# TIMELINE

**工时概览**：题目要求的全部功能（6 端点、ETL、agent、验收脚本、README/REPORT）在
**12:00–16:43，约 4 小时 40 分**内完成并首次推送（见下方逐块记录）；17:11 之后的时间全部是
**超出题面的打磨与增强**（真 token 级流式、会话历史侧栏、URL 路由、每消息独立 WS、文档
一致性修订、移除 /ask），不属于题面功能的实现耗时。

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

07-15 17:11-18:05  修复 /chat 真 token 级流式（agent_node 内接 event_sink 队列桥接，不再等
                    ainvoke 返回后一次性推送）。调试过程中用原始 WS 探针复现出一个更严重的
                    真实 bug：工具结果里带 Postgres NUMERIC(Decimal) 时 send_json 序列化
                    崩溃、整条 WS 连接无 close frame 直接死——这正是"前端看起来不流式/卡住"
                    的部分根因。修复(自定义 JSON encoder + send_text) + 3 个回归测试。
                    期间还确认了 chrome-devtools 的 list_network_requests 不显示 WS 连接，
                    属工具限制，之前用它的空输出当论据是错的，已收回。
                    人类：坚持"看不到流式/看不到 WS"的实测观察，不接受 AI 的错误辩解，
                    直接推动查到真 bug；要求去掉打字机光标特效只留流式。
                    AI：WS 探针复现、抓全栈 traceback、修复、回归测试。(commit 18:05)

07-15 18:05-18:17  WS 连接模型第一次修改：从"进页面就连"改为懒连接（首次发消息才握手），
                    解决"进 chat 页白白挂一条连接"的问题。
                    人类：指出进页面就看到 WS 不合理。AI：实现 + 验证。(commit 18:17)

07-15 18:17-18:47  两线并行：①熵减（另一个审计 agent 的重复代码报告落地：共享 LLM 构造、
                    /ask 的 usage 日志 try/finally 化、密码长度常量去重；用户明确约束
                    "计算逻辑不动"，两处涉及计算/prompt 的合并建议被搁置）；②后台子 agent
                    实现会话历史功能（title/updated_at 列、GET /chat/sessions、checkpoint
                    历史回放端点、前端侧栏），真实浏览器验证，顺带修掉一个跨测试模块
                    事件循环绑定导致的既有 flaky。审计 agent 曾建议删除"冗余的 /ask"，
                    被驳回——/ask 是题目 6 个必做端点之一，不因前端不调用而删。
                    人类：给出熵减范围约束；驳回删 /ask 的建议；要求会话历史功能。
                    AI：主控做熵减+验收，子 agent 做历史功能。(证据 18:44)

07-15 18:47-19:09  用户指出侧栏"都是 New conversation 没法区分"+ URL 必须带 session id。
                    落地 /chat/:sessionId 路由 + NULL title 从 checkpoint 首条消息回填。
                    排查"前端连不上 API"时发现真凶：一个周日起就在宿主机裸跑的
                    python -m app.main 进程一直占着 8000 端口、间歇性遮蔽 Docker 里的真 API
                    ——杀掉后 IPv4/IPv6 全通。
                    人类：指出侧栏可用性问题、要求显式路由。AI：实现 + 排查端口占用。

07-15 19:09-19:48  参考另一项目(legal_web)的 WS 模式，按用户要求把连接模型第二次重构：
                    每条消息开一条全新 WS、回合结束即关——不再有跨回合长连接，也就不再需要
                    重连/心跳状态机。过程中修掉两个真 bug：①currentAnswerMessage 声明位置
                    在 immediate watcher 之后，带历史的会话每次刷新都 TDZ ReferenceError；
                    ②新会话查历史必 404 刷 console 红字，改为后端对从未用过的 session id
                    返回 200+[]（语义上"还没有消息的对话"，不泄露信息）。
                    人类：给出参考项目、拍板每消息一连接、逐条追问异常输出（404、空
                    session_state、拒答文案）直到解释/修复清楚。
                    AI：重构 + 浏览器实测 + 修 bug。(refactor commit 19:48)

07-15 19:48-20:00  提交门禁（本仓库 .claude/skills 配置的 pre-commit 流程）：auto-test-writer
                    要求补齐未覆盖函数——给 title 回填补了 3 个测试，当场逮到一个真 bug
                    （commit 后访问被 server-onupdate expire 的属性 → MissingGreenlet），
                    修复后全量 218 通过；code-review-eval 走完最小化/副作用/破坏性/覆盖
                    检查（纯样式改动拆独立 commit），风险 Low；/chat 验收脚本 5/5（含重启
                    存活）。4 个 commit 推送 fork。(commits 19:48-20:00)

07-15 20:00-20:40  收尾：核对 CHALLENGE.md 全项完成度；ARCHITECTURE.md 全部图表更新为
                    论文图格式（图 1-7 带图题）并用真实 mermaid v11 解析器验证 7/7；修掉
                    §4.1 与 §4.4 的框架选型自相矛盾（旧"不用框架"立场残留）；session 标题
                    改为完整存储（截断只在展示层）。
                    人类：逐条指出文档失真（左右互搏、截断存储、/ask 定位）。
                    AI：修订 + 验证 + 提交推送。

07-15 20:40-       移除 /ask：人类拍板删除整个端点（路由/schema/测试）。作为题目要求
                    无可厚非，但这个 /ask 对于本系统的功能与架构来说比较冗余——/chat 的
                    agent 经 run_readonly_sql 覆盖同一 NL→SQL 能力且更强，两条并行管线
                    只有维护成本。属明知题面必做仍做出的主动偏离（详见 REPORT.md
                    "移除 /ask"、ARCHITECTURE.md 偏离②）。README/HARNESS/ARCHITECTURE
                    全部文档同步对齐，7 类示例问题改经 /chat 演示。
                    人类：删除代码、给出定性（"冗余，我删的"）。
                    AI：文档全面对齐 + 回归验证 + 提交。
```
