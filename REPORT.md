# REPORT

## DB / LLM 选型

**DB：PostgreSQL**（不是 SQLite）。demo 数据只有 32 个 ASIN，但按目标生产规模（百万级价格
历史/多用户并发写）设计——SQLite 单写者模型在 Celery 多 worker 并发刷新时会成为瓶颈，
Postgres 给真索引、真并发写、JSONB。这条在过程中被明确纠正过一次：最初按"demo 小、4 小时
预算"倾向 SQLite，被否决——"虽然 demo 只有这些数据，但实际场景会有百万级"，工程判断应该按
目标规模走，不是按当前样本量走。

**LLM：DeepSeek**（OpenAI 兼容接口）。技术上底层走 `ChatOpenAI` 打 OpenAI 兼容协议，换供应商
只需要改 `LLM_BASE_URL`/`LLM_MODEL`，不改代码——具体选 DeepSeek 是因为国内访问快、SQL/代码
生成能力够用、成本低。

## 两条技术决策记录

### ① 会话记忆的存储方案

**选项**：
1. 进程内内存 dict — 简单，但 `docker compose restart` 直接清空，不满足题目"重启存活"的
   硬性要求，直接否决
2. 手写 Postgres 表（`chat_sessions.active_filters`/`user_preferences` 表）— 最初的方案，
   自己维护消息历史、工具调用记录、偏好表
3. **LangGraph 的 Checkpointer（短期/session 级）+ Store（长期/user 级）**— 最终选择

选 ③ 的过程：手写方案（②）实现到一半时发现一个真实缺陷——工具调用是代码"猜"出 intent 后
自己悄悄执行的，LLM 并不知情，`chat_messages` 表里也没有对应记录，等于对话推理链路丢了一段，
没法审计/回放。调研后发现 LangGraph 的 Checkpointer/Store 正好是这个问题的官方解法：
Checkpointer 按 `thread_id`（=`session_id`）自动持久化完整消息历史（含 `tool_calls`/
`ToolMessage`），Store 按 `namespace=("preferences", user_id)` 做跨 session 的长期记忆——
跟我们要的"短期记忆 session 级、长期记忆 user 级"语义完全对上，而且是库自己管表
（`checkpoints`/`checkpoint_writes`/`checkpoint_blobs`/`store`），不用再维护一份会跟它打架
的重复表。`chat_sessions` 表因此瘦身成纯粹的 user 归属校验（session 属于哪个登录用户），不
再存业务状态。

**规模上限**：Checkpointer/Store 都是 Postgres 后端，单机单 Postgres 实例扛到中等并发没问题；
真到需要多地域/多租户隔离的规模，会需要按 `namespace`/`thread_id` 做分片或换成专门的会话存储。

### ②`/refresh` 后台任务的实现方案

**选项**：
1. `FastAPI BackgroundTasks` — 简单但没有进度追踪，容器重启后无法感知"跑到哪了"，不满足
   断点续跑要求，否决
2. Celery + Redis broker/worker — 最终选择
3. Celery Beat 定时（每天 04:00 UTC）复用同一套防重入逻辑

选 Celery 的理由：`/refresh` 要满足"不阻塞请求 + 防重入 + 断点续跑 + 单点失败隔离"，
`BackgroundTasks` 这几条一条都保证不了。断点续跑的核心设计是`_start_refresh()`一个内部函数
被三处共用（手动 `POST /refresh`、Celery Beat 定时触发、进程重启后重新触发）：检查是否已有
`state='running'` 的 job，有就把该 job 里还是 `pending` 的 `refresh_job_items` 重新入队（不是
简单"已有任务就什么都不做"——worker 被杀死后 DB 会永远停在 `running`，必须能continue 而不是
卡死）；已完成的 item 靠 `WHERE state='pending'` 的原子更新保证不被重复处理。这个设计里真发现
一个 bug：`POST /refresh` 的防重入检查本身允许同一个 pending item 被两次入队（这是"防重入
允许对已有 job 重新派发"这条设计本身导致的边界情况），`_record_item_result` 一开始没做
`WHERE state='pending'` 的原子守卫，导致 `done+failed` 会超过 `total`（实测 36 vs 32）——
加了原子 `UPDATE ... WHERE state='pending'` + rowcount 检查后修复，加了回归测试。

**规模上限**：单 Redis broker + 若干 Celery worker，几十到几百 ASIN、分钟级刷新没问题；
ASIN 上千、要更强的任务队列健壮性（死信队列、优先级队列）时，这套已经是 Celery 的能力范围
内可以直接加的，不需要换框架。

## 一次 prompt 迭代

**v1**：`/ask` 的 triage system prompt 只给了"SQL / DIRECT / OUT_OF_SCOPE"三选一的粗粒度
指引，"DIRECT: 定义性问题不用查数据"这条描述不够精确。

**失败 case**：
1. `"Why doesn't B006JVZXJM qualify as eligible?"`（针对具体 ASIN 的解释类问题）——模型判成
   DIRECT，给了一段通用解释而不是真的查这个 ASIN 的 `filter_failed` 字段，等于在编答案
2. `"If you had to pick one ASIN to resell right now, which would it be and why?"`（主观推荐类）
   ——模型的回答是"我没法做主观推荐，我也没有访问你实际数据的权限"，直接拒答/甩锅，完全没有
   走 SQL 路径

两个 case 根因相同：模型把"听起来像在解释/给建议"误判成"不需要查数据"，但 CHALLENGE.md 明确
要求这两类都必须 grounded 在真实数据上。

**v2**：在 prompt 里显式加了两条纠偏——① 只要问题点名了具体 ASIN 或涉及真实数据，即使听起来
像解释类，也必须走 SQL，不能走 DIRECT；② 明确告诉模型"你确实有实时查询数据的能力，不要说
自己没权限/没法访问数据"，主观推荐类问题必须先查 SQL 拿真实候选再回答。改完后
`tests/test_ask_examples.py` 15/15 用真实 DeepSeek 调用跑通（含这两个具体 case）。

（后记：`/ask` 端点连同该测试文件在提交收尾阶段被整体移除——见下方"移除 /ask"——这段
prompt 迭代如实保留为历史记录；两条纠偏教训同样适用于 `/chat` 的 SYSTEM_PROMPT，其
grounding 规则从一开始就包含"只引用真实查询结果、不编造数据"。）

## 移除 /ask（主动的题面偏离，人为拍板）

题目把 `POST /ask` 列为必做端点，本项目曾完整实现并测试过它（独立的两次-LLM-调用管线）。
提交收尾阶段人为决定**整体移除**：作为题目要求无可厚非，但对本系统的功能与架构而言它是
冗余的——`/chat` 的 agent 经 `run_readonly_sql` 工具覆盖了完全相同的
NL→SQL→安全校验→执行→grounded 回答能力，且更强（多轮、状态、偏好）；两条并行 NL→SQL
管线只有维护成本没有产品价值。移除范围：路由、schema、对应测试；SQL 安全校验不受影响
（`validate_readonly_sql` 全仓库唯一定义，工具层测试 `test_tool_run_readonly_sql.py`
持续覆盖）。题目 7 类示例问题在 README 改经 `/chat` 演示，效果相同。此前一轮代码审计曾
建议删除、被以"题面必做"驳回——这次是明知题面要求仍选择架构一致性，评审如认定按题面扣分，
接受。

## 脏数据处理策略

- Keepa 的 `-1` 一律当"无数据"处理成 `None`，不当 0（`app/keepa/parse.py::safe_value`），
  所有解析函数统一走这一个函数，避免某处漏判
- 缺字段（如 `fbaFees` 整个缺失）防御性 `.get()`，不 KeyError
- `eligible`/`ROI` 依赖的字段有 `None` 时，不编造：ROI 缺任一必需字段就是 `None`（不是 0），
  `amazon_buybox_pct` 缺失时该条规则按"没有证据说明被 Amazon 主导"处理为 pass（不是默认 fail）
- ETL/refresh 单 ASIN 处理失败不传染：`_process_product` 内部用 SAVEPOINT 隔离，一条错不影响
  同批其余 ASIN，计入 `error`/`failed` 计数而不是让整批失败

## 成本核算

`llm_usage_log` 表记录每次 LLM 调用的 input/output/total tokens（`get_usage_metadata_callback`
本地聚合，不依赖 LangSmith 云端），`scripts/cost_report.py` 汇总输出 + 调 Keepa 免费的
`GET /token` 查余额。本次开发+联调过程中 DeepSeek 消耗见 `candidate_package/test_evidence/
phase6/cost_report_output.txt`（这一项数字随开发过程持续增长，以脚本实测输出为准，不在这里
写死一个会过期的数字）。**真实 Keepa 消耗：97 tokens**（一次 `python -m app.etl` 批量拉
32 个 ASIN，`stats=90&buybox=1&fbafees=1`，单批 1 次 Keepa 请求，token 池从 269 降到 172，
约 3 token/ASIN；证据见 `candidate_package/test_evidence/phase7/real_keepa_verification.txt`）。
开发过程中的大部分时间这个环境访问不了 `api.keepa.com`（详见下方"故意没做好的地方"关于网络
限制的说明），中途网络恢复后补跑了一次真实 ETL + `/refresh`，两者都以 32/32 成功收尾。两个
Keepa key 共享同一个 token 池（约 600 + 5/min 回血），非独立额度，多 key 轮换解决的是限流
问题而不是扩大总额度。

## 我故意没做好的地方

- **`/refresh` 没有角色/权限系统**——任何登录用户都能触发全量刷新，只做了 `triggered_by`
  审计字段，没做权限限制。这是单人评审的 take-home，不是多租户 SaaS，故意不做
- **ETL/refresh 每次都是全量重拉**，不会跳过"数据还新鲜"的 ASIN——在 Keepa token 有限的
  真实场景下这是浪费，故意没加"只刷新过期数据"的逻辑，优先把断点续跑本身做对
- **`plan_combo` 的类目多样性是假的**——`asins` 表没有真实 category 字段（Keepa 也没有干净
  的单一类目字段），用标题/ASIN 哈希分桶出 8 个伪类目，在模块文档里明确标注这是近似值不是
  真实亚马逊类目
- **`/upc` 对 Bookland/ISBN 条码（978/979 前缀）没有本地换算兜底**——识别出该前缀后策略是
  "不做任何变换、原样透传 Keepa"（避免错误地按普通 UPC-A 剥零），实测这足够让题目的
  required 用例 case_03 通过（Keepa 自己认 ISBN-13，返回了 ISBN-10 `054546529X`，证据见
  `phase7/verify_upc_real_network.txt`）。没做的是"若 Keepa 不认 EAN-13 时本地剥 978 前缀+
  重算 ISBN-10 校验位"这层防御性转换——当前依赖 Keepa 的行为，属故意留下的兜底缺口
- **问到库里没有的 ASIN 时不会现场调 Keepa 补拉**（题目加分项）——`lookup_asin` 直接回
  "not found in catalog"。做对它需要处理限流/token 预算/并发去重，故意不在 4 小时预算里做
- **`/chat` 消息没有幂等去重**（题目加分项）——同一条消息被客户端重试重发会执行两次、状态
  重复累积。当前前端"每消息一条 WS + 回合内禁止再发"把这个窗口收得很小，但服务端没有兜底
- **eligibility 阈值硬编码在 `app/eligibility.py` 常量里**（题目加分项是配置化）——5 条规则
  的阈值直接来自题面且题目声明"照着写"，配置化收益低，故意不做
- **LLM 结果缓存没做**（题目加分项）——`/chat` 的问题重复率在真实使用里不可预知，
  缓存键设计（问题归一化+数据版本）本身就是个工程，demo 阶段收益存疑
- **开发过程中大部分时间连不上 `api.keepa.com`**（DNS 一度解析到网络测试用的哨兵地址）——
  不是代码问题，是开发所在网络环境的出站限制（诊断细节：DNS 在同一网段下把其他域名正常代理
  过去、唯独 `api.keepa.com` 连接建立后卡在 TLS 握手，像是网关按域名分类做的出站策略）。这段
  时间所有 Keepa 相关验证走 respx mock（单测）或灌进真实 ETL pipeline 的合成 fixture 数据。
  网络后来间歇恢复，补跑了真实 `python -m app.etl`（32/32 成功）、真实 `POST /refresh`
  （32/32 成功）、以及多轮真实 `scripts/verify_upc.py`——**累计 7 个用例里 6 个拿到过真实
  PASS**（含 `hard` 加分项 case_04 的 GTIN-14 补零，返回 `122308079X`），唯一没 PASS 的
  case_06 是全 9 假条码、按题目设计就该查不到。抖动的直观证据：同晚相隔几分钟的两轮运行，
  一轮 case_01–04 过/05–07 断，另一轮只有 case_07 过——同一份代码，结果完全跟着出网窗口走
  （`phase7/verify_upc_rerun_2300.txt` 两轮原始输出都在）。这也是为什么不把网络当成
  "已解决"，提交前仍建议在稳定网络下完整跑一遍

## AI 工具坦白

本项目使用 **Claude Code**（Anthropic；主体阶段模型为 Claude Sonnet 5，收尾阶段人工切换到
Claude Fable 5）完成，采用**多子 agent 编排**的
工作方式：我（人类）负责架构决策（DB/队列/鉴权模型/LLM 供应商选型、是否强制鉴权、Postgres
vs SQLite 等）、对每个阶段的产出做验收判断、发现问题时决定修复方向；Claude Code 主控负责把
决策拆解成阶段任务、给每个子 agent 写详细的任务简报、独立复核每个子 agent 的产出（不是只信
自述——实测跑 `docker compose up`、真实 curl、真实 `pytest`、用 chrome-devtools MCP 操作
真实浏览器截图）；子 agent 负责具体的代码实现（模型/路由/ETL/LangGraph agent/前端组件/验收
脚本）。绝大部分代码是 AI 生成的；人类工作是方向判断、验收把关、以及少量直接改动（如 `/ask`
的 prompt 迭代是我读了失败 case 后自己改的两版）。

## 如果有更多时间


- 加真实的 refresh 增量刷新（只重拉过期数据），节省 Keepa token
- 根据用户偏好做用户画像管理
- 根据用户量和数据量做服务器容灾
- 对数据做分库分表操作，目前少量数据可能只需要20 - 30 ms，实际生产环境绝对是需要按照 类目/地区/时间 进行分库分表
- 优化前端到生产级
- 添加模型回退路由(类似lite llm)
- `plan_combo` 通过爬虫换成真实类目数据源
- ISBN/Bookland 条码的正确转换规则
- 给 `/refresh` 加最小可用的角色/权限模型(涉及管理员后台权限管理)
