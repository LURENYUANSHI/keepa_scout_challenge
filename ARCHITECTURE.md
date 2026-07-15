# Architecture — Keepa Scout

设计文档，不含实现代码。覆盖：系统部署架构、数据模型关系、核心流程时序图。

> **对 CHALLENGE.md 的一处偏离，先说清楚**：题目给的 `/upc`、`/eligibility`、`/ask`、
> `/chat`、`/refresh` 等 curl 示例都不带 `Authorization` header。本设计决定加**强制鉴权**、
> **不做匿名回退**——意味着所有业务端点都必须先 `POST /auth/register` + `POST /auth/login`
> 拿到 token 才能调用。README 里的 curl 示例会相应地在最前面加一步"注册/登录拿 token"。
> 这是主动做的产品化决策，超出题目字面要求，会在 REPORT.md 里作为"额外做了什么、为什么"写清楚。

---

## 1. 系统部署架构

```mermaid
flowchart TB
    subgraph Client
        C[浏览器 / curl / Loom 演示]
    end

    subgraph Compose["docker compose"]
        FE["frontend\n(nginx, Vue SPA 静态产物)"]
        API["api\n(FastAPI, uvicorn)"]
        WORKER["worker\n(Celery worker)"]
        BEAT["beat\n(Celery beat — 每天 04:00 UTC 定时刷新)"]
        DB[("db\nPostgreSQL")]
        BROKER[("broker\nRedis")]
    end

    subgraph External["外部服务"]
        KEEPA["Keepa Product API"]
        LLM["LLM Provider\n(OpenAI 兼容接口)"]
    end

    C -->|"加载 SPA (静态资源)"| FE
    C -->|"HTTP + Bearer token；\nWS /chat/stream（每条消息一条连接，\n回合结束即关，见 §3.5）"| API
    API -->|"读写: users / auth_tokens / chat_sessions /\nasins / asin_price_stats / refresh_jobs /\nrefresh_job_items / llm_usage_log"| DB
    API -->|"checkpointer/store 读写\n(短期状态+长期偏好)"| DB
    API -->|"派发任务"| BROKER
    WORKER -->|"消费任务"| BROKER
    WORKER -->|"写快照 / 价格统计 / 任务进度"| DB
    WORKER -->|"批量拉取"| KEEPA
    BEAT -->|"每天 04:00 UTC 调用与 POST /refresh\n相同的内部函数，复用同一把防重入锁"| BROKER
    API -->|"NL→SQL 生成 / 回答生成"| LLM
```

**图 1** 系统部署架构。六个 compose 服务：`frontend` 只分发静态产物，浏览器拿到 SPA 后直连
`api`；`api` 不在请求线程里跑长任务，全量刷新经 `broker` 交给 `worker`；`beat` 与手动
`POST /refresh` 复用同一个内部入口。会话记忆（checkpointer/store）与业务表同库存放。

**职责边界**：
- `api` 只做请求校验、鉴权、读写 DB、把耗时的全量刷新甩给 Celery——不在请求线程里跑长任务
- `worker` 是唯一真正调用 Keepa 批量接口的地方（`/refresh` 触发的全量刷新);`/upc`、`/eligibility`
  这种单次查询由 `api` 直接同步调 Keepa（延迟可接受，不需要排队)
- **断点续跑的真相源在 Postgres 的 `refresh_job_items`，不在 Redis**——即使 broker 数据丢失，
  依然能从 DB 里查出哪些 ASIN 还没完成本轮刷新
- **`beat` 不是一条独立的业务逻辑**，它只是定时调用 `POST /refresh` 背后同一个"启动刷新任务"的
  内部函数——防重入检查（是否已有 `state=running` 的 job）天然复用，不用为定时任务单独写一套判断；
  如果 04:00 UTC 时手动触发的刷新还没跑完，beat 这次触发就是空操作（等价于并发 `POST /refresh`）

---

## 2. 数据模型关系（ER）

```mermaid
erDiagram
    USERS ||--o{ AUTH_TOKENS : "issues"
    USERS ||--o{ CHAT_SESSIONS : "owns"
    USERS ||--o{ LLM_USAGE_LOG : "incurs"
    USERS ||--o{ REFRESH_JOBS : "manually triggers"
    REFRESH_JOBS ||--o{ REFRESH_JOB_ITEMS : "tracks"
    ASINS ||--o| ASIN_PRICE_STATS : "has 90d stats"
    REFRESH_JOB_ITEMS }o--|| ASINS : "refreshes"

    USERS {
        uuid id PK
        text email "UNIQUE，按 lower(email) 去重"
        text password_hash
        timestamptz created_at
    }
    AUTH_TOKENS {
        text token_hash PK "存 token 的哈希，不存明文"
        uuid user_id FK
        timestamptz created_at
        timestamptz expires_at
        timestamptz revoked_at "NULL = 有效"
    }
    CHAT_SESSIONS {
        text session_id PK
        uuid user_id FK "NOT NULL，不允许匿名"
        text title "可空；首条用户消息全文（截断只发生在前端展示层），只设置一次；NULL 时列表接口从 checkpoint 回填"
        timestamptz created_at
        timestamptz updated_at "每轮对话 touch，会话列表按它倒序"
    }
    LLM_USAGE_LOG {
        bigserial id PK
        uuid user_id FK
        text session_id "可空——/ask 没有 session"
        text endpoint "ask / chat"
        text model
        int input_tokens
        int output_tokens
        int total_tokens
        timestamptz created_at
    }
    ASINS {
        text asin PK
        text title
        numeric buybox
        numeric referral_fee_pct
        bigint sales_rank
        numeric amazon_buybox_pct
        numeric monthly_sold
        boolean eligible
        text filter_failed
        numeric computed_roi_pct
        numeric supplier_cost
        timestamptz snapshot_at
    }
    ASIN_PRICE_STATS {
        text asin "PK, FK"
        numeric avg_90d
        numeric min_90d
        numeric current_deviation_pct
        timestamptz computed_at
    }
    REFRESH_JOBS {
        text job_id PK
        text state "running/done"
        text trigger_source "manual / scheduled"
        uuid triggered_by "FK -> users.id，可空——scheduled 触发时为 NULL"
        int total
        int done
        int failed
        timestamptz started_at
        timestamptz finished_at
    }
    REFRESH_JOB_ITEMS {
        text job_id FK
        text asin FK
        text state "pending/done/failed"
        timestamptz updated_at
    }
```

**图 2** 数据模型（ER）。LangGraph 自建的 `checkpoints`/`checkpoint_writes`/`checkpoint_blobs`/
`store` 表不在图中重复建模（见图题下方第二条约束与 §4）；`chat_sessions` 是业务侧唯一与会话
相关的表，承担归属校验与列表元数据（`title`/`updated_at`）两件事。

**关键约束**：
- `chat_sessions.user_id` **NOT NULL**——没有匿名会话，一个 session 必须属于一个登录用户
- `chat_sessions` 做**归属校验 + 会话列表元数据**两件事：归属（这个 session_id 是不是这个
  user 的）、`title`（首条用户消息**全文**——截断只发生在前端展示层，一次性设置，NULL 时由
  `GET /chat/sessions` 从 checkpoint 首条 HumanMessage 回填并持久化）、`updated_at`
  （每轮 touch，列表排序键）。
  **不存** `active_filters`/`last_result_asins` 等短期状态——那些字段连同完整的消息/工具调用记录，由
  **LangGraph 的 Checkpointer**（`AsyncPostgresSaver`，按 `thread_id = session_id`）自动持久化，
  它会在同一个 Postgres 实例里自建 `checkpoints`/`checkpoint_writes`/`checkpoint_blobs` 表，
  本图不重复建模那几张表（细节见 §4）
- 长期偏好（`budget_per_unit`/`excluded_asins`）不再是这里的 `user_preferences` 表，改由
  **LangGraph 的 Store**（`AsyncPostgresStore`，`namespace=("preferences", user_id)`）持有——
  这是 §4 调研后定的：Store 天然按 user_id 分区、跨 thread，跟我们要的"长期记忆"语义完全对上，
  没必要再手动维护一张会跟 Store 打架的重复表
- **`/ask` 不查用户偏好**——`/ask` 的 SQL 生成只面向 `asins`/`asin_price_stats`，偏好类问题
  （"我的预算是多少"）走 `/chat` 由 agent 通过 Store 回答；这条把 §4 留下的开放问题定了，Store
  是唯一数据源，不做 SQL 可查的镜像表，避免两个来源打架
- `auth_tokens` 存的是 token 的哈希（如 SHA-256），不存明文——DB 泄露不等于 token 泄露；原始
  token 只在签发那一刻返回给客户端一次
- `refresh_jobs.triggered_by` 可空——手动 `POST /refresh` 时记触发人，`beat` 每天 04:00 UTC
  定时触发时为 NULL、`trigger_source='scheduled'`，用于审计"这次刷新是谁/什么触发的"，不代表
  权限限制（见下方"角色"备注）
- **没有角色/权限系统**——任何登录用户都能触发 `/refresh`，这是有意的范围限制（这是单人评审的
  take-home，不是多租户 SaaS），`triggered_by` 只做审计，不做权限校验；写进 REPORT.md"故意没做
  好的地方"清单

---

## 3. 核心流程

### 3.1 注册 + 登录

```mermaid
sequenceDiagram
    participant C as Client
    participant API as api
    participant DB as Postgres

    C->>API: POST /auth/register {email, password}
    API->>DB: SELECT 1 FROM users WHERE lower(email)=?
    alt 邮箱已存在
        API-->>C: 409 Conflict
    else 邮箱可用
        API->>API: bcrypt.hash(password)
        API->>DB: INSERT INTO users (...)
        API->>API: 生成随机 token，计算 hash
        API->>DB: INSERT INTO auth_tokens (token_hash, user_id, expires_at)
        API-->>C: 201 {access_token, expires_at}
        Note over API: 长期偏好无需占位行——LangGraph Store 按<br/>("preferences", user_id) 惰性创建（见 §4）
    end

    C->>API: POST /auth/login {email, password}
    API->>DB: SELECT id, password_hash FROM users WHERE lower(email)=?
    alt 用户不存在 或 密码不匹配
        API-->>C: 401 Unauthorized
    else 校验通过
        API->>API: 生成新 token，计算 hash
        API->>DB: INSERT INTO auth_tokens (...)
        API-->>C: 200 {access_token, expires_at}
    end
```

**图 3** 注册与登录。注册即签发 token（免二次登录）；登录失败对"用户不存在"与"密码错误"
返回同一响应，不泄露邮箱注册状态。

### 3.2 受保护端点的统一鉴权（所有业务端点都走这一步）

```mermaid
sequenceDiagram
    participant C as Client
    participant API as api (依赖注入 get_current_user)
    participant DB as Postgres

    C->>API: 任意业务请求 + Authorization: Bearer <token>
    alt 缺少 header 或格式不对
        API-->>C: 401 Unauthorized
    else 带 token
        API->>API: hash(token)
        API->>DB: SELECT user_id, expires_at, revoked_at\nFROM auth_tokens WHERE token_hash=?
        alt 找不到 / revoked_at 非空 / 已过期
            API-->>C: 401 Unauthorized
        else 有效
            API->>API: 把 user_id 注入 request context
            Note over API: 继续走具体端点逻辑
        end
    end
```

**图 4** 统一鉴权依赖（`get_current_user`）。token 只存哈希，比对同样先哈希再查表。

**受保护范围**：除 `POST /auth/register`、`POST /auth/login` 外，`/upc`、
`/eligibility/{asin}`、`/eligibility/batch`、`/ask`、`/chat`、`WS /chat/stream`
（token 经 query 参数传入，握手前校验，失败以 4401 关闭）、`GET /chat/sessions`、
`GET /chat/sessions/{id}/messages`、`/refresh`、`/refresh/status` 全部要求这一步通过。

### 3.3 `/chat`——短期记忆 + 长期偏好 + 工具调用记录如何合并进一次回答

```mermaid
sequenceDiagram
    participant C as Client
    participant API as api
    participant DB as Postgres(chat_sessions 归属表)
    participant G as LangGraph(checkpointer + store)
    participant LLM as LLM Provider

    C->>API: POST /chat {session_id, message} + Bearer token
    API->>API: 鉴权 → user_id
    API->>DB: SELECT * FROM chat_sessions WHERE session_id=?
    alt session 不存在
        API->>DB: INSERT chat_sessions (session_id, user_id,<br/>title=首条消息全文, updated_at=now())
        Note over API: 新会话，归属当前登录用户
    else session 存在但 user_id 不匹配
        API-->>C: 403 Forbidden
        Note over API: 防止用别人的 session_id 越权读写
    else session 存在且归属正确
        API->>DB: UPDATE chat_sessions SET updated_at=now()<br/>（title 仍为 NULL 时顺带补设，只设一次）
    end

    API->>G: graph.ainvoke({message}, config={thread_id: session_id, user_id})
    G->>G: checkpointer 按 thread_id 取出历史消息(含之前的 tool_calls/结果)
    G->>G: store.get(("preferences", user_id)) 取长期偏好
    G->>LLM: messages(system + 短期状态摘要 + 长期偏好摘要 + 本轮 message)\ntools=[build_filter_sql, lookup_asin, plan_combo,\nrun_readonly_sql, update_preferences, reset_topic]
    LLM-->>G: AIMessage(tool_calls=[...]) 或直接文本(含拒答)
    G->>G: checkpointer 自动把这条 AIMessage 存进 thread 历史

    alt 没有 tool_calls（out_of_scope 拒答 / 无需查数据的直接回复）
        Note over G: 短期状态原样保留，走到最后一步
    else 有 tool_calls（ToolNode，最多 N 轮，代码里设上限防止失控循环）
        loop 依次执行每个 tool_call
            G->>G: 按该 tool 的 JSON Schema 校验参数\n(结构化工具只收结构化参数；run_readonly_sql 额外过\nSELECT-only/单语句/禁DDL-DML 安全校验)
            G->>DB: 执行只读查询 / 纯确定性计算(见 4.2 工具清单)
            G->>G: checkpointer 把 ToolMessage(tool_call_id, 结果) 存进 thread 历史
            opt 是 update_preferences
                G->>G: store.put(("preferences", user_id), key, value)
            end
        end
        G->>LLM: messages + 上面所有 ToolMessage
        LLM-->>G: 最终 AIMessage：自然语言 answer(引用具体 ASIN/指标)
        G->>G: checkpointer 存下这条最终回答
    end

    G->>API: usage_metadata(每次 LLM 调用的 input/output/total tokens)
    API->>DB: INSERT llm_usage_log (user_id, session_id, endpoint='chat', model, tokens...)
    G-->>API: answer + 本轮 session_state(从 checkpoint 最新状态里读出)
    API-->>C: {answer, results, session_state}
```

**图 5** `/chat` 单轮编排（两种传输共用同一编排）。图中以 `POST /chat`（同步返回）表示；
`WS /chat/stream` 走完全相同的编排，区别只在返回方式：每个工具调用发生的当刻推送
`tool_call_start`/`tool_call_result` 事件对，最终回答按 token 级 `answer_delta` 增量推送
（`answer_done` 收尾，罕见情况下用 `answer_retract` 撤回被误判为答案的工具前独白），
回合以 `session_state`（成功）或 `error`（失败）事件收束。连接生命周期见图 7。

**为什么这样才对**：上一版里 `build_filter_sql`/`lookup_asin`/`plan_combo` 是代码在"猜"到某个 intent
之后自己悄悄调用的，LLM 并不知情，消息记录也没地方存——等于对话里发生了什么只有代码自己知道，
回放/审计/调试都做不到。现在这些操作全部包装成 LLM 原生 `tool_calls`，模型自己决定调不调、调哪个、
传什么参数；持久化这部分不用我们手写表和 INSERT 语句了——LangGraph 的 checkpointer 会把
"user → assistant(tool_calls) → tool(结果) × N → assistant(最终回答)" 这条完整链路自动按
`thread_id` 存进 Postgres，可以整段回放，我们只需要在 `chat_sessions` 里管住"这个 thread 是谁的"
这一件事。

### 3.4 `/refresh`——防重入 + 断点续跑

```mermaid
sequenceDiagram
    participant C as Client
    participant BEAT as Celery beat
    participant API as api（_start_refresh 内部函数）
    participant DB as Postgres
    participant BR as Redis (broker)
    participant W as Celery worker
    participant K as Keepa API

    alt 手动触发
        C->>API: POST /refresh + Bearer token
        API->>API: _start_refresh(trigger_source='manual', triggered_by=user_id)
    else 定时触发（每天 04:00 UTC，可配置）
        BEAT->>API: 直接调用 _start_refresh()——不经过 HTTP/鉴权层
        API->>API: _start_refresh(trigger_source='scheduled', triggered_by=NULL)
    end

    API->>DB: SELECT 1 FROM refresh_jobs WHERE state='running'
    alt 已有任务在跑
        API-->>C: 200 {job_id: <既有job>, state: running}（定时触发命中这条分支时就是空操作，无 HTTP 响应）
        Note over API: 防重入——两种触发路径共用同一次检查，不用为定时任务单独写一套判断
    else 没有任务在跑
        API->>DB: INSERT refresh_jobs (job_id, state='running', trigger_source, triggered_by, total=N)
        API->>DB: INSERT refresh_job_items (job_id, asin, state='pending')\nFOR EACH asin NOT IN 上一轮已完成
        API->>BR: 为每个 pending 的 asin 派发 Celery task
        API-->>C: 202 {job_id, state: running, total, done: 0}
    end

    par 并发执行
        W->>BR: 取任务
        W->>K: 批量拉取 ASIN 数据
        K-->>W: product 数据
        W->>W: 计算 eligibility / ROI / 90天统计
        W->>DB: UPSERT asins, asin_price_stats
        W->>DB: UPDATE refresh_job_items SET state='done' WHERE asin=?
        W->>DB: UPDATE refresh_jobs SET done=done+1
    end

    C->>API: GET /refresh/status + Bearer token
    API->>DB: SELECT * FROM refresh_jobs WHERE job_id=?
    API-->>C: {state, total, done, failed, last_refresh_at}

    Note over C,W: === 此处 docker compose kill 再 up ===

    C->>API: POST /refresh (重新触发)
    API->>DB: SELECT 1 FROM refresh_jobs WHERE state='running'
    Note over API: 上一个 job 的 state 还停在 'running'\n（进程被杀没来得及标记完成）
    API->>DB: SELECT asin FROM refresh_job_items\nWHERE job_id=<上一个job> AND state != 'done'
    Note over API: 只把没完成的 asin 重新派发\n已完成的不重拉——幂等
    API->>BR: 重新派发 pending 任务
```

**图 6** `/refresh` 防重入与断点续跑。续跑的真相源是 Postgres 的 `refresh_job_items`
（不是 Redis）；`done+failed` 计数由 `UPDATE ... WHERE state='pending'` 的原子守卫保证
不越界（该守卫来自一次实测越界 bug 的修复，见 REPORT.md）。

### 3.5 会话历史与"每条消息一条 WS"的连接生命周期

```mermaid
sequenceDiagram
    participant B as 浏览器 (Vue SPA)
    participant API as api
    participant DB as Postgres(chat_sessions)
    participant G as LangGraph(checkpointer)

    Note over B: 进入 /chat/:sessionId —— session id 在 URL 里，<br/>刷新/前进后退/分享链接都落回同一会话；<br/>裸 /chat 由前端路由重定向到新生成的 id
    B->>API: GET /chat/sessions
    API->>DB: SELECT ... WHERE user_id=? ORDER BY updated_at DESC
    opt 有 title 为 NULL 的行（早期数据）
        API->>G: aget_state(thread_id) 取首条用户消息
        API->>DB: 回填 title 并持久化（每行只发生一次）
    end
    API-->>B: 会话列表（侧栏：标题 + 相对时间）

    B->>API: GET /chat/sessions/{id}/messages
    alt session 属于他人
        API-->>B: 403
    else 从未使用过的新 id 或 存在但无 checkpoint
        API-->>B: 200 []（"还没有消息的对话"是一等状态，不是 404）
    else 有历史
        API->>G: aget_state(thread_id)
        API-->>B: 200 [user/tool/answer 消息序列]（工具卡按 tool_call_id 配对结果）
    end

    Note over B: 用户点击发送 —— 此刻才建立连接
    B->>API: 新建 WS /chat/stream?token=... （每条消息一条全新连接）
    API-->>B: tool_call_start / tool_call_result / answer_delta ... （见图 5）
    API-->>B: session_state（回合结束）
    B->>B: 关闭本条 WS，状态回到 idle
    Note over B,API: 回合之间没有存活连接——无需心跳/重连状态机；<br/>中途断线即本回合失败，重发消息=新连接重试
```

**图 7** 会话历史加载与每回合独立的 WebSocket 生命周期。历史回放是纯 REST，绝不建立 WS；
连接只在"发送→回合结束"窗口内存在（该模型的取舍讨论见 REPORT.md 与 useChatSocket.js 头注释）。

---

## 4. Agent / LLM 编排技术

### 4.1 结论先行：LangGraph 做薄编排 + 原生 tool calling，且每次调用都要落库

**最终选型是 LangGraph**（一个 StateGraph：agent 节点 `bind_tools` + tools 节点 + 条件边 +
`MAX_TOOL_ROUNDS` 上限），配 LLM 原生 `tools` / function-calling。

**这条结论推翻过本节的上一版立场**，如实记录：最初的判断是"不上任何 Agent 框架、手写
tool-calling loop"（理由是工具集很小、框架是调试黑盒、手写更好在 Loom 里讲清楚）。这个立场
被两件事推翻：① 手写方案实现到一半发现工具调用链路会丢——代码在猜出 intent 后悄悄调用工具，
LLM 并不"知道"这些调用存在，消息表里也没有记录，审计/回放/多轮工具衔接都做不到；② 题目
"重启存活"是硬性要求，手写路线意味着自己维护一套消息/工具调用/状态的持久化表，而调研发现
LangGraph 的 Checkpointer/Store 正是这个问题的官方解法（细节见 §2 与 REPORT.md 决策记录①）。
换到 LangGraph 后，原立场里真正重要的三条原则**原样保留**：

- **框架用得薄**：只用 StateGraph + checkpointer/store 这一层，不上 CrewAI / AutoGen 式
  多 agent 交接——工具集固定、每轮调用次数有上限，图结构小到可以在 Loom 里逐节点讲清楚
- 工具的参数校验、SQL 安全校验是**确定性代码、每次都跑**（在 tools 节点的代码路径里，
  不依赖框架内部"决定要不要校验"的黑盒逻辑）
- **对话里发生的每一步都必须是可回放的**——LLM 决定调用哪个工具、传了什么参数、工具返回了
  什么，必须是持久化消息历史里一条独立的记录（`AIMessage` 带 `tool_calls`，`ToolMessage` 带
  `tool_call_id`+结果；由 checkpointer 落库，`GET /chat/sessions/{id}/messages` 的历史回放
  直接消费这份记录——图 7）

模型自己决定要不要调用工具、调哪个、传什么参数（不是代码替它决定），工具参数按 JSON Schema
强制校验，校验失败/字段缺失时**代码里把错误反馈给模型重试一次**（不是让模型自己决定要不要
重试），超过重试次数直接判定该轮解析失败，走兜底话术，不假装成功。

### 4.2 工具清单——LLM 能"做"的每一件事都是一个具名、带 Schema、会被记录的工具

| 工具 | 参数（结构化，不接受原始 SQL） | 实现 | 会话状态影响 |
|---|---|---|---|
| `build_filter_sql` | `min_roi?`, `eligible_only?`, `max_amazon_pct?`, `max_supplier_cost?`, `sort?`, `limit?` | 代码按模板拼 SQL 执行（只读） | 合并/替换进 `active_filters`，刷新 `last_result_asins` |
| `lookup_asin` | `asin?` 或 `reference: {ordinal?, pronoun?}` | 代码解析 reference（用 session 的 `last_result_asins`/`resolved_entity`），查详情 | 更新 `resolved_entity` |
| `plan_combo` | `budget`, `diversify_categories?`, 其余约束 | 代码跑贪心/背包算法，纯确定性 | 不改 filters，只影响这轮回答内容 |
| `run_readonly_sql` | `sql: string` | **唯一接受原始 SQL 的工具**；代码过 SELECT-only/单语句/禁 DDL-DML 校验后执行 | 视查询内容可能刷新 `last_result_asins` |
| `update_preferences` | `budget_per_unit?`, `exclude_asin?`, `note?` | `store.put(("preferences", user_id), ...)`，替换/追加语义见 HARNESS.md §7.1 | 不改短期状态，改的是长期偏好（跨 session 生效） |
| `reset_topic` | 无参数 | 清空 `active_filters`/`last_result_asins`/`resolved_entity` | 短期记忆清零 |

**分工原则不变，只是落实方式变了**：凡是能算准的（SQL 怎么拼、组合怎么选、指代怎么解析、
价格异常怎么判断）都在工具的**代码实现**里，不让 LLM 自己算；LLM 负责的是"这轮该不该调用
工具、调哪个、传什么参数"以及"看到工具结果后怎么讲人话"。区别在于：这个决策过程现在是显式
的 `tool_calls`，不是代码从自由文本里猜出来的隐藏 intent。

`run_readonly_sql` 是 `/chat` agent 图里**唯一**接受原始 SQL 文本的工具；其余工具只接受
结构化参数，把"安全校验"这个高风险面收窄到一个入口。

**`/ask` 的位置要单独说清楚（题目必做端点，保留，但故意不走这张图）**：CHALLENGE.md 把
`/ask` 描述成一个固定的两步模式——"LLM 生成一条 SQL → 安全校验 → 执行 → 第二次 LLM 调用
把结果格式化为有依据的回答"，无 `session_id`、无多轮状态。让它复用 `/chat` 的完整 agent 图，
等于为一个单发请求支付一次性 `thread_id`、ToolMessage 往返和只会走一条路径的条件边循环，
纯属浪费。所以 `/ask` 是独立的轻量两次-LLM-调用管线（`app/routers/ask.py`），**不经过**
LangGraph；但它 import 的是与 `run_readonly_sql` 工具完全相同的
`validate_readonly_sql`/`run_readonly_sql_impl`——"什么 SQL 允许执行"这个安全判定全仓库
仍然只有一处定义。另一个已定案的边界：`/ask` 只做后端（curl/README/Loom 演示），前端
**故意**不给它做页面——Chat 页在交互上完全覆盖了自然语言提问，给被覆盖的端点做 UI 才是冗余
（题目明示不评判前端）。曾有一轮代码审计建议"删除冗余的 /ask"，被驳回：它是题目 6 个必做
端点之一，是否保留不取决于前端调不调它。

### 4.3 单轮 `/chat` 的编排步骤

见 3.3 的时序图——那张图已经是完整版本（含鉴权、checkpointer/store 加载、tool_calls 往返、
成本记录、最后的状态返回），这里不重复画，避免两张图各改一半、互相漂移。

### 4.4 技术栈清单

- LLM/Agent 框架：`langgraph` + `langgraph-checkpoint-postgres`（`AsyncPostgresSaver` 短期记忆、
  `AsyncPostgresStore` 长期偏好）；模型调用走 LangChain 的 `ChatOpenAI`（配 `base_url` 打 OpenAI
  兼容接口，沿用"换供应商只换 base_url"的决定）——用 LangChain 的模型封装是为了拿到
  `bind_tools`/`usage_metadata`/checkpointer 这套配套设施，不是额外加一层
- 工具参数校验：Pydantic v2 model 对应每个工具的 JSON Schema，`model_validate_json` 失败即
  代码内把错误信息反馈给模型重试一次
- SQL 安全校验：手写正则 + 关键字黑名单检查（只认单条 `SELECT`，无分号叠加语句，无
  DDL/DML 关键字），不引入额外 SQL parser 依赖，只用在 `run_readonly_sql` 这一个入口
- 组合规划算法：手写贪心/局部背包即可，不上 PuLP/OR-Tools——ASIN 是几十到几百量级，上专业
  优化库是过度工程
- 成本核算：**已决定**——`get_usage_metadata_callback()`（LangChain 内置，本地聚合，不依赖
  LangSmith 云端）挂在每次 graph 调用上，把 input/output/total tokens 写进 `llm_usage_log`
  表（见 §2 ER 图）；纯后端，不做前端页面（见 HARNESS.md §9）

## 5. 项目目录结构

前面几节的表/图/工具最终要落到具体文件上，这里给出对应关系，`HARNESS.md` 里出现的所有路径
（`app/etl.py`、`scripts/verify_chat.sh`、`tests/test_tool_*.py` 等）都以这份结构为准。

```
keepa_scout_challenge/
├── app/                        # 后端 FastAPI 应用
│   ├── main.py                  # FastAPI 入口，挂载全部 router（对应 §1 部署图的 api 服务）
│   ├── config.py                # 环境变量加载（.env）
│   ├── db.py                    # async SQLAlchemy engine/session
│   ├── models/                  # ORM 模型，一一对应 §2 ER 图里的表
│   │   ├── user.py               # users, auth_tokens
│   │   ├── asin.py               # asins, asin_price_stats
│   │   ├── chat.py               # chat_sessions（归属校验 + title/updated_at 列表元数据，见 §2）
│   │   ├── refresh.py            # refresh_jobs, refresh_job_items
│   │   └── usage.py              # llm_usage_log
│   ├── schemas/                 # Pydantic request/response model
│   ├── routers/                 # 按端点分组
│   │   ├── auth.py                # POST /auth/register, /auth/login
│   │   ├── upc.py                 # GET /upc
│   │   ├── eligibility.py         # GET /eligibility/{asin}, POST /eligibility/batch
│   │   ├── ask.py                 # POST /ask —— 独立两次-LLM-调用管线，不走 agent 图（§4.2 末段）
│   │   ├── chat.py                # POST /chat + WS /chat/stream + GET /chat/sessions[/{id}/messages]
│   │   │                          #   —— 鉴权/归属/历史回放，实际编排调用 agent/graph.py
│   │   └── refresh.py             # POST /refresh, GET /refresh/status
│   ├── auth/
│   │   ├── security.py            # 密码哈希、token 生成/校验(哈希存储，见 §2)
│   │   └── dependencies.py        # get_current_user，FastAPI Depends，§3.2 时序图落地处
│   ├── agent/                   # §4 Agent/LLM 编排技术落地处
│   │   ├── graph.py               # StateGraph 定义：agent 节点(bind_tools) + ToolNode + 条件边
│   │   ├── tools.py               # §4.2 六个工具的具体实现
│   │   ├── checkpointer.py        # AsyncPostgresSaver 初始化（FastAPI lifespan 里建立）
│   │   ├── store.py               # AsyncPostgresStore 初始化
│   │   └── usage.py               # get_usage_metadata_callback 封装，写 llm_usage_log
│   ├── keepa/                   # Keepa 客户端
│   │   ├── client.py              # httpx client + 双 key 轮换 + 402/429 退避
│   │   └── parse.py               # csv[]/stats 下标解析、keepaTime 换算、-1 兜底
│   ├── eligibility.py           # 5 条规则 + compute_roi/compute_payout（题目给定公式，原样实现）
│   ├── etl.py                   # `python -m app.etl` 入口，对应 §1 Dockerfile 的启动流程
│   └── tasks/                   # Celery（对应 §1 部署图的 worker/beat 服务）
│       ├── celery_app.py          # Celery app，broker=Redis；beat_schedule 里配每天 04:00 UTC 的 cron
│       └── refresh_tasks.py       # _start_refresh() + 单 ASIN 刷新 task；§3.4 断点续跑/定时触发逻辑落地处
│
├── frontend/                   # Vue SPA（§10 HARNESS.md）
│   └── src/
│       ├── views/                # 登录/注册/Dashboard(eligibility)/UPC/Refresh/Chat 页面
│       ├── components/
│       ├── composables/          # useChatSocket.js —— 每回合独立 WS 生命周期（图 7）
│       ├── router/               # /chat/:sessionId 路由；裸 /chat 重定向到新 session id
│       ├── api/                  # 后端接口封装（token 注入、WS base URL 推导）
│       ├── utils/                # session id 生成、markdown 渲染
│       └── stores/                # 登录态（Pinia）
│
├── data/                        # 题目提供，只读，ETL 输入
│   ├── sample_asins.csv
│   └── upc_test_cases.json
│
├── tests/                       # pytest；§7.1 工具清单单测都在这
│   ├── test_eligibility_rules.py
│   ├── test_etl_dirty_data.py
│   ├── test_ask_examples.py
│   ├── test_ask_sql_injection.py
│   ├── test_chat_websocket.py    # WS 流式协议（真实 LLM 调用）
│   ├── test_chat_sessions.py     # 会话列表/回放/标题回填（图 7 的服务端行为）
│   └── test_tool_*.py            # 每个工具一个文件，对应 HARNESS.md §7.1 表格
│
├── scripts/                     # HARNESS.md 里引用的验收脚本，都是可独立运行的黑盒脚本
│   ├── verify_all.sh
│   ├── verify_auth.sh
│   ├── verify_upc.py
│   ├── verify_chat.py            # 题目要求的必交验收脚本之一
│   ├── verify_refresh_resume.sh  # 题目要求的必交验收脚本之一
│   └── cost_report.py
│
├── candidate_package/           # 题目原始材料，不改动
├── ARCHITECTURE.md              # 本文档
├── HARNESS.md                   # 验收口径
├── README.md                    # 必交
├── REPORT.md                    # 必交
├── TIMELINE.md                  # 必交
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

**几个容易混的点先说清楚**：
- `app/agent/` 是 §4 那一整套 LangGraph 编排（tools/graph/checkpointer/store/usage）唯一的家，
  `app/routers/chat.py` 只负责鉴权和把请求转给 `agent/graph.py`，不掺业务逻辑
- `app/tasks/` 和 `app/routers/refresh.py` 是两个不同职责：`routers/refresh.py` 处理 HTTP 请求
  （建 job、查状态），真正跑 Keepa 拉取和写库的是 `tasks/refresh_tasks.py`（Celery worker 进程里跑）
- `tests/` 和 `scripts/` 也是两类不同的证据：`tests/` 是 `pytest` 能直接发现和跑的单测/集成测试，
  `scripts/` 是需要真实起容器/服务的黑盒验收脚本（HARNESS.md 里那些要 `docker compose kill` 之类
  的场景，没法用 pytest 表达，只能是独立脚本）

## 6. 收尾细节（已全部定案）

- `email` 唯一，走 `UNIQUE INDEX ON lower(email)`（不引入 `citext` 扩展，多一个依赖没必要）
- `/auth/register` 自动登录——注册成功直接签发 token，不要求再单独调一次 `/auth/login`
- **token 过期时长：24h，过期后重新 `/auth/login` 拿新 token**——不做 refresh token 机制。
  理由：这是一个提交后评审在 72 小时窗口内会多次 `docker compose up`/操作的 demo 项目，24h
  够覆盖单次演示/评审 session，短过期比长期免登录更能体现"有在乎安全"，refresh token 的复杂度
  在这个规模上不值得
- **密码策略：最小 8 位，不做大小写/数字/符号组合的强制要求**——不是消费级产品，不需要企业级
  密码策略；同时 bcrypt 输入超过 72 字节会被截断，注册时对超长密码直接拒绝（返回 400），不能
  静默截断导致用户以为设了个更长的密码
