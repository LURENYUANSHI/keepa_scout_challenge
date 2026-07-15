# Harness — Keepa Scout

验收口径文档：每一块能力写清楚 **目标 (Objective)** / **验收标准 (Acceptance Criteria)** /
**证据 (Evidence)**。"证据"必须是可以被别人重新跑一遍、拿到确定性结果的东西（命令 + 期望输出 /
脚本 + 期望退出码 / SQL 查询 + 期望行数），不是"我看着感觉对"。

技术栈：后端 Python（FastAPI + async SQLAlchemy + Postgres + Celery + LangGraph），前端 Vue。
本文覆盖后端全部能力项 + 前端的最小可用性验收；架构细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

> **CHALLENGE.md 里的场景对话（原 `/ask` 的 7 类问题、`/chat` 场景 A–G）是例子，不是字面 spec**。
> 用 LangGraph 把 NL→SQL/意图识别包装成工具之后，验收不能靠"把文档里的英文原句抄进测试断言、
> 让它字符串匹配过关"——那样只是在拟合示例，遇到换一种问法就会露馅。第 6/7 节的验收标准和证据
> 都要求用**意译过的问法**去验证，且覆盖场景背后的能力（累积/替换/指代/持久化...），不是逐字
> 复现文档例句。

---

## 如何跑这份 harness

```bash
docker compose up --build -d          # 起全部服务，ETL 自动跑
./scripts/verify_all.sh               # 依次跑下面每一节"证据"里列的脚本，汇总 PASS/FAIL
```

`verify_all.sh` 是所有独立验收脚本的编排入口，不重复实现校验逻辑，只负责按顺序调用 + 汇总退出码。
题目要求的两个必交验收脚本（`/chat` 多轮、`/refresh` 断点续跑）包含在其中，也可以单独跑：

```bash
./scripts/verify_chat.sh
./scripts/verify_refresh_resume.sh
```

---

## 0. 部署

**目标**：`docker compose up --build` 一键起服务，ETL 自动跑，:8000 对外暴露全部端点。

**验收标准**：
- 全新环境（`docker compose down -v` 之后）执行 `docker compose up --build` 无需任何手动步骤
- API 在 :8000 就绪，ETL 在容器启动流程里自动执行一次
- 重复执行 `docker compose up --build`（数据已存在）不产生重复行、不报错

**证据**：
- `docker compose up --build` 的日志里出现 `Uvicorn running on http://0.0.0.0:8000` 且退出码为 0（前台不退出，用 healthcheck 判定）
- `GET /health`（无需鉴权）返回 200
- 二次执行前后 `SELECT count(*) FROM asins` 数值不变（幂等）

---

## 1. 鉴权（注册 / 登录 / 强制鉴权）

**目标**：邮箱+密码账号体系，所有业务端点强制鉴权，不允许匿名回退（见 ARCHITECTURE.md 的偏离说明）。

**验收标准**：
- `POST /auth/register` 成功后自动签发可用 token；同邮箱重复注册 → 409
- `POST /auth/login` 密码错误 → 401；成功 → 返回新 token
- 任意业务端点缺 token / token 过期 / token 被撤销 → 401
- 用 A 用户的 token 访问 B 用户拥有的 `session_id` → 403（不能靠猜 session_id 越权读写）

**证据**：
```bash
# 见 scripts/verify_auth.sh，核心断言：
TOKEN=$(curl -s -X POST :8000/auth/register -d '{"email":"a@x.com","password":"..."}' | jq -r .access_token)
curl -s :8000/chat -d '{"session_id":"s1","message":"hi"}'                      # 期望 401（不带 token）
curl -s :8000/chat -H "Authorization: Bearer $TOKEN" -d '...'                   # 期望 200
curl -s -X POST :8000/auth/register -d '{"email":"a@x.com",...}'                # 期望 409（重复邮箱）
# 用 B 用户 token 访问 A 用户的 session_id → 期望 403
```

---

## 2. `/upc`

**目标**：UPC/EAN 输入做 normalization（补零/剥包装位/多变体重试），让脏输入也能查到 ASIN；一个
UPC 对应多个 ASIN 时全部返回。

**验收标准**：
- `data/upc_test_cases.json` 里全部 7 个测试输入，最终都能拿到非空的 `asins` 数组
- 已知对应多个 ASIN 的 UPC，返回数组长度 > 1（不是只取第一个）

**证据**：
```bash
python scripts/verify_upc.py   # 遍历 upc_test_cases.json，逐条 curl /upc，打印每条的 PASS/FAIL 和实际 asins
```
脚本退出码非 0 视为验收失败；README 里附一份跑这个脚本的输出截图/日志。

---

## 3. `/eligibility/{asin}` + `/eligibility/batch`

**目标**：eligibility 5 条规则 + ROI 公式严格按题目给定实现，不自己发明算法。

**验收标准**：
- 5 条规则里第一条失败的原因准确写进 `filter_failed`
- `computed_roi_pct` 与题目给的 `compute_payout`/`compute_roi` 公式逐位一致
- `/eligibility/batch`：按输入顺序返回；混入 Keepa 查不到的 ASIN 不 500，优雅返回该项的 null/错误标记；150+ ASIN 自动分块（不因为超过 Keepa 单批上限而整体失败）

**证据**：
```bash
pytest tests/test_eligibility_rules.py   # 拿固定输入手算的期望值和 compute_roi()/compute_payout() 输出做逐位比对
curl :8000/eligibility/B006JVZXJM        # 已知因 rank 超阈值不合格的 ASIN，断言 filter_failed == "rank"
curl -X POST :8000/eligibility/batch -d '{"asins":["<valid>","DOES_NOT_EXIST","<valid>"]}'
# 断言：返回数组顺序与输入一致；中间那项不是 500，是明确的"未找到"标记
```

---

## 4. ETL (`app/etl.py`)

**目标**：批量拉 Keepa、算 eligibility/ROI/90天统计、幂等 upsert 进 Postgres。

**验收标准**：
- 同样输入跑两次，`asins` 表最终状态完全一致（不重复插入、不产生脏行）
- 单次 Keepa `/product` 请求塞多个 ASIN（不是每个 ASIN 单独发一次请求）
- Keepa 返回的 `-1`（无数据）字段不会被当成 0 处理，也不会让 ETL 崩掉

**证据**：
```bash
python -m app.etl && psql -c "SELECT asin, updated_at FROM asins ORDER BY asin" > /tmp/run1.txt
python -m app.etl && psql -c "SELECT asin, updated_at FROM asins ORDER BY asin" > /tmp/run2.txt
diff /tmp/run1.txt /tmp/run2.txt   # 期望除 updated_at 外完全一致，行数不变
grep "batched .* ASINs in .* Keepa calls" logs/etl.log   # 期望调用次数 < ASIN 数
pytest tests/test_etl_dirty_data.py   # 喂一个 -1 / null 字段的 mock Keepa 响应，断言不抛异常且字段被标记为"无数据"而非 0
```

---

## 5. 90 天价格统计 + 数据新鲜度 + 价格异常

**目标**：`asin_price_stats` 存 90 天均价/最低价/当前偏离度；回答里体现数据时点；超 24h 提醒过期；
buybox 明显偏离 90 天均值时提示异常。

**验收标准**：
- 每个成功刷新过的 ASIN 在 `asin_price_stats` 里有对应行
- `/eligibility`、`/chat` 的数据型回答带 `snapshot_at` 或等价的"数据时点"说明
- 快照 > 24h 时回答文案里出现过期提醒（形式不限，但要出现）
- buybox 相对 90 天均值偏离超过设定阈值时回答里出现异常提示

**证据**：
```bash
curl :8000/eligibility/<asin> | jq .snapshot_at                   # 非空
psql -c "UPDATE asins SET snapshot_at = now() - interval '26 hours' WHERE asin='<asin>'"
curl :8000/eligibility/<asin> | grep -i "26h ago\|stale\|refresh"  # 期望命中过期提醒文案
psql -c "UPDATE asins SET buybox = avg_90d * 3 ..."                # 人为制造价格异常
curl :8000/eligibility/<asin> | grep -i "anomaly\|deviat"          # 期望命中异常提示文案
```

---

## 6. NL → SQL 能力（原 `/ask`——端点已移除，能力由 `/chat` 承接）

> `POST /ask` 已整体移除（ARCHITECTURE.md 文档开头"偏离②"：功能上是 `/chat` 的严格子集，
> 属主动的题面偏离）。本节验收的是**能力**而不是端点：LLM 生成只读 SQL → 代码安全校验 →
> 执行 → grounded 回答；域外拒答；边界问题不误杀。这些如今全部走 `/chat` 的
> `run_readonly_sql` 工具路径。

**验收标准**：
- 破坏性 SQL 尝试（"Drop the asins table..."）不会真的执行，`asins` 表行数不变
- 题目给的 7 类示例问题（计数/单一filter/复合filter/解释类/主观推荐/业务判断/域外拒答）经
  `/chat` 提问都产出 grounded、引用真实 ASIN/指标的回答
- 边界问题（"ROI 是什么？"）不被误判为 out_of_scope

**证据**：
```bash
pytest tests/test_tool_run_readonly_sql.py   # SQL 安全校验的工具层单测：非 SELECT/多语句/DDL-DML 全部拒绝
python3 scripts/verify_chat.py               # 多轮验收里含域外拒答不丢上下文场景
# 7 类问题的 /chat 版 curl 示例见 README「自然语言提问」一节
```

---

## 7. `/chat`（多轮、工具调用、重启存活）

**目标**：LangGraph 驱动的多轮 agent，覆盖题目 8 种上下文模式；工具调用通过 checkpointer 持久化
（不会像早期设计那样丢失记录，见 ARCHITECTURE.md §4.1）；短期记忆（session）与长期偏好（Store，
按 user_id）分离；`docker compose restart` 后状态不丢。

### 7.1 工具清单——6 个工具各自单独验收，不合并成一条

场景级验收（7.2）测的是端到端行为，测不出"是哪个工具的问题"。这里对每个工具单独定
objective/AC/evidence，绕开 LLM 直接调工具函数本身，出问题能直接定位。

| 工具 | 目标 | 验收标准 | 证据 |
|---|---|---|---|
| `build_filter_sql` | 结构化 filter → 安全模板 SQL | 只认白名单字段（`min_roi`/`eligible_only`/`max_amazon_pct`/`max_supplier_cost`/`sort`/`limit`），未知字段被忽略不是透传；SQL 值一律走参数化，不做字符串拼接 | `pytest tests/test_tool_build_filter_sql.py`：直接调工具函数，喂一组 filter dict 断言生成的 SQL/参数；喂一个多余字段断言被忽略且不报错 |
| `lookup_asin` | 指代（序数/代词/显式 ASIN）→ 具体 ASIN 详情 | "the second one" 正确取 `last_result_asins[1]`（1-based"第二个"→ index 1）；序数越界（只有 3 条结果却问"第10个"）返回明确的越界提示，不是 crash 或返回错误 ASIN | `pytest tests/test_tool_lookup_asin.py`：固定 `last_result_asins` fixture，`ordinal=2` 断言取到正确 asin；`ordinal=99` 断言返回越界错误而非 `IndexError` |
| `plan_combo` | 预算约束下的组合规划 | 总花费不超预算；排除 `user_preferences.excluded_asins`；要求跨类目时类目数 > 1；**确定性**——同样输入跑两次结果完全一致（没有随机性/没有偷偷调 LLM 算数字） | `pytest tests/test_tool_plan_combo.py`：固定 catalog + budget=500，断言 `sum(subtotal) <= 500` 且涉及类目数 > 1；同输入连跑两次 diff 输出，断言完全相同 |
| `run_readonly_sql` | 全仓库唯一接受原始 SQL 的入口 | 拒绝多语句（`;` 分隔的第二条语句）；拒绝 DDL/DML 关键字（大小写混合、注释里藏关键字等变体都要拒）；只有单条 `SELECT` 会被执行 | `pytest tests/test_tool_run_readonly_sql.py`：参数化一组恶意 SQL（`DROP TABLE`/`; DELETE FROM asins`/`SELECT ...; DROP TABLE asins--`/`dRoP TaBLe`），断言全部在执行前被拒；一条合法多行 `SELECT` 断言正常执行 |
| `update_preferences` | 长期偏好 UPSERT（Store） | `budget_per_unit` 是**替换**语义（连续两次设置 20 → 50，最终是 50 不是叠加/平均）；`excluded_asins` 是**追加**语义（第二次排除另一个 ASIN，第一个不会被顶掉） | `pytest tests/test_tool_update_preferences.py`：连续调用两次不同 budget，断言最终值是最后一次；连续调用两次不同 exclude_asin，断言 Store 里两个都在 |
| `reset_topic` | 清空短期记忆，不动长期偏好 | 调用后 `active_filters`/`last_result_asins`/`resolved_entity` 清空；`user_preferences`/Store 内容不受影响 | `pytest tests/test_tool_reset_topic.py`：先设置 filters + preferences，调用 `reset_topic`，断言 filters 类字段清空、preferences 原样保留 |

**工具调用可观测性**（补充，属于 checkpointer 而不是单个工具的验收）：每一次工具调用在
LangGraph checkpoint 里都能查到对应的 `tool_calls`/`ToolMessage` 记录，不是黑盒——证据见 7.2
末尾的 `psql` 查询。

### 7.2 多轮场景验收（对应 CHALLENGE.md 场景 A–G，例子不是字面 spec，见文档开头 callout）

**验收标准**：
- A. filter 累积：连续追加条件，结果集正确收窄
- B. 指代消解："the second one"/"it" 正确指向上一轮结果
- C. 主题切换 + OOS 不丢上下文：拒答天气后，下一轮仍能恢复之前的话题
- D. 阈值替换：新阈值替换旧阈值，不是叠加
- E. 偏好持久化：budget 等约束在后续查询里生效，**换一个新 session_id（同一用户）依然生效**（验证 Store 是按 user_id 不是按 session_id）
- F. 纠错持久化：排除某 ASIN 后，`docker compose restart`，同一 `session_id` 继续对话，排除依然生效
- G. 组合规划：多约束下给出预算内、跨类目、有依据的具体组合

**证据**：
```bash
./scripts/verify_chat.sh
# 每个场景至少一版用意译问法(不是抄 CHALLENGE.md 原句)，验证的是能力(累积/替换/指代/持久化)本身
# 内部依次跑场景 A-D、G（同一 session 内可完成）
# 场景 E：先建 session s-e1 设置偏好，再开新 session s-e2（同一用户 token），断言 s-e2 里偏好依然生效
# 场景 F：设置纠错 → docker compose restart（脚本内真的执行这一步）→ 用同一 session_id 继续对话 → 断言排除依然生效
# 断言均基于 answer/results/session_state 的具体字段值，不是"看起来像"

psql -c "SELECT checkpoint FROM checkpoints WHERE thread_id='<session_id>' ORDER BY checkpoint_ts DESC LIMIT 1" \
  | grep -i "tool_calls"   # 证明工具调用确实进了 checkpoint，不是只存了最终回答
```

---

## 8. `/refresh` + `/refresh/status` + 每日定时刷新

**目标**：Celery 驱动的全量后台刷新；不阻塞请求；防重入；断点续跑；单点失败不拖累整批；每天
04:00 UTC 由 Celery beat 自动触发一次，跟手动触发共用同一套防重入逻辑（见 ARCHITECTURE.md §3.4）。

**验收标准**：
- `POST /refresh` 在 1 秒内返回 `job_id`+`state=running`（不等全部拉完才返回）
- 刷新进行中再次 `POST /refresh` 不开第二个任务（返回同一个 `job_id`）
- 进行到一半 `docker compose kill` 再 `up`，重新触发后：已完成的 ASIN **不重拉**（`updated_at` 不变），只处理剩余的
- 人为让某一个 ASIN 的 Keepa 请求失败，不影响其余 ASIN 继续处理，计入 `failed`
- `celery beat` 的 schedule 配置里存在一条每天 04:00 触发的 cron 项，指向跟 `POST /refresh` 相同的内部函数
- 定时触发落库的 `refresh_jobs.trigger_source='scheduled'` 且 `triggered_by IS NULL`；手动触发的
  `trigger_source='manual'` 且 `triggered_by` 是发起请求的 `user_id`
- 定时触发命中"已有任务在跑"分支时是纯粹的空操作，不产生第二个 job（不需要真等到第二天来验证，
  见下面证据的做法）

**证据**：
```bash
./scripts/verify_refresh_resume.sh
# 内部：POST /refresh → 轮询 /refresh/status 到 done 达到一部分 →
# 记录此时已完成的若干 asin 的 updated_at → docker compose kill && docker compose up -d →
# 重新 POST /refresh → 轮询到 state=done → 断言:
#   1) 之前已完成的 asin updated_at 没有变化(没有被重拉)
#   2) 最终 done + failed == total
#   3) 期间没有出现第二个 running 状态的 job_id

curl -X POST :8000/refresh; curl -X POST :8000/refresh   # 连续两次，断言两次返回同一个 job_id
psql -c "SELECT trigger_source, triggered_by FROM refresh_jobs ORDER BY started_at DESC LIMIT 1"
# 断言手动触发那条 trigger_source='manual' 且 triggered_by 非空

pytest tests/test_scheduled_refresh.py
# 不真等到 04:00：直接单测调用 _start_refresh(trigger_source='scheduled', triggered_by=None)，
# 断言落库字段正确；另起一个已有 running job 的 fixture 状态，调用同一函数，断言不产生第二个 job
python -c "from app.tasks.celery_app import app; print(app.conf.beat_schedule)"
# 断言里面有一条 cron='0 4 * * *' 的 schedule 项，指向 _start_refresh
```

---

## 9. 成本核算（`llm_usage_log`，纯后端，暂不做前端页面）

**目标**：`/chat` 的每次 LLM 调用都记录 token 用量（本地 `usage_metadata`，不依赖 LangSmith
云端，见 ARCHITECTURE.md §4），配合 Keepa 自己的 `/token` 接口，服务 REPORT.md 的成本核算要求。
**明确不做前端 dashboard/图表**——一条脚本输出到终端就够，Vue 那边不需要对应页面，避免范围蔓延。

**验收标准**：
- 每次 LLM 调用后 `llm_usage_log` 增加一行，字段包含 input/output/total tokens
- 有一条命令能把整个 demo 期间的 LLM token 总量 + Keepa token 消耗汇总输出

**证据**：
```bash
python scripts/cost_report.py
# 输出形如:
# LLM:   12,340 input / 3,102 output / 15,442 total tokens across 41 calls
# Keepa: tokensLeft=214, refillRate=5/min (来自 GET https://api.keepa.com/token)
```

---

## 10. 前端（Vue）

题目不评判前端美观，但"不评判"不等于"随便"——这里主动加了个质量下限：不能一眼看出是 AI 随手
生成的模板界面。拆成三块：功能可用性 / 视觉不能有 AI 味 / 流式渲染健壮性。做的时候用
`frontend-design` skill 出视觉方案，不要让模型凭默认审美自由发挥。

### 10.1 功能可用性

**目标**：最小可用的 Vue SPA，覆盖登录/注册 + 6+1 个端点的操作入口 + `/chat` 会话界面，用于 Loom
演示。

**验收标准**：
- 登录/注册在浏览器里能走通，token 能正确带到后续请求
- 每个必做端点在 UI 上有对应操作入口，点击后能看到接口返回的数据
- `/chat` 页面同一次浏览会话里 `session_id` 保持不变，多轮对话上下文正确

**证据**：
- Loom 视频里现场操作一遍（主要证据来源，UI 类验收难以完全自动化）
- （可选加分）`scripts/verify_frontend.py`：Playwright 跑一遍登录 → 提问 `/chat` → 断言页面上出现
  预期文本，作为回归用的烟雾测试，不追求覆盖率

### 10.2 视觉质量——禁止"AI 味"

**目标**：界面看起来是有人做过设计判断的，不是模型套默认模板糊出来的。

**验收标准（黑名单，命中任意一条算不通过）**：
- 蓝紫渐变色背景/按钮/卡片（`linear-gradient` 从蓝到紫，或紫色系 `#6366f1`/`#8b5cf6` 一类的
  "AI 紫"作主色）
- 每个卡片/区块下面都莫名其妙挂一行灰色小字 description，内容是对标题的同义反复、不提供
  任何新信息（比如标题"ASIN 列表"，下面又写一行"查看你的 ASIN 列表"）
- 大量圆角+阴影+emoji 图标堆砌，没有信息层级
- 默认 shadcn/Tailwind 调色板不做任何取舍地全量用上（间接等于没做过取色决策）

**证据**：
- 走查清单：截图过一遍，逐条比对上面黑名单，命中就改
- README 里贴 2–3 张关键页面截图，供人工复核

### 10.3 流式渲染健壮性——`/chat` 工具调用的坑

**目标**：`/chat` 走 LangGraph 的流式输出（工具调用事件应该是边产生边推给前端），前端要正确按事件
增量渲染，而不是等一整轮跑完再一次性展示；同时要扛得住流式连接本身失败的情况。这几个是已知的
典型坑，明确列出来防止踩：

**验收标准**：
1. **工具调用要一个一个吐出，不能等全部工具调用完了再一次性推给前端**——即模型这一轮决定调用
   3 个工具时，UI 应该依次看到 3 次"调用中→有结果"的状态变化，不是等 3 个都跑完之后突然一起冒出来
2. **工具调用渲染完之后，DOM 里不能留下莫名其妙的空 `<div>`**——常见成因是"调用中"的占位组件
   在收到最终结果后没有被正确替换/卸载，只是把文字清空但节点还在；每次工具调用渲染结束后检查
   DOM，不应该存在没有文本内容、没有子元素、也不是有意做间距用的空 div
3. **WebSocket/SSE 流式连接失败要有明确处理**——断线/超时不能让聊天框卡死转圈或什么反应都没有：
   要么自动重连+提示"重新连接中"，要么明确降级成一次性非流式请求重试，总之不能是静默失败

**证据**：
```bash
# 手动/半自动验证，记录进 Loom 或验收脚本里：
# 1) 触发一次会调用 2+ 工具的问题（比如组合规划场景），录屏确认工具卡片是逐个出现，不是同时蹦出来
# 2) 该轮结束后，浏览器 devtools 里跑:
document.querySelectorAll('div')  // 遍历，断言不存在 innerHTML.trim() === '' 且 children.length === 0 的非预期空 div
# 3) 用 devtools 网络面板手动切断 WS 连接(或 kill 后端容器)，观察前端是否给出明确的重连/失败提示，
#    而不是无响应；恢复连接后能否继续对话
```

---

## 非目标（不在这份 harness 里验收）

跟 CHALLENGE.md 的"我们不评判"对齐，避免验收标准喧宾夺主：
- 前端视觉的精致度/像素级打磨（10.2 只是一条不能太差的下限，不是往上限去卷）
- 成本核算的前端可视化（明确不做，见第 9 节）
- 测试代码覆盖率（够用的 sanity test 即可，不要求覆盖率数字）
- 具体选了哪个 LLM 模型
