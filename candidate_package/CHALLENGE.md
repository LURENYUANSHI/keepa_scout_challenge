# 笔试题：Amazon Arbitrage Scout

请实现一个基础但完整的 Web 服务，让 Amazon 卖家可以识别有利可图的进货
机会、按业务规则评估 ASIN、并通过支持多轮上下文的 AI 助手用自然语言提问。

UI 保持最简（原始 HTML 或 JSON 响应即可，不做任何样式）。

请使用 **Python 3.11+、FastAPI、async SQLAlchemy、SQLite 或 PostgreSQL、
Keepa Product API、任一 LLM 服务商**（海外的 OpenAI / Anthropic / Gemini，
或国内的 DeepSeek / Moonshot Kimi / 通义千问 Qwen / 智谱 GLM / 豆包 / Yi
等都可以，自选）。

**提交期限：** 收到本说明后 **72 小时内** 提交。任务设计为在 **≤ 4 小时**
内完成（可以使用 Claude Code / Cursor / Copilot 等 AI 辅助工具）。如果
4 小时内没完成也没关系 —— 提交已完成的部分，在 `REPORT.md` 中说明剩下的
计划即可。

---

## 关于 AI 工具

允许使用 Claude Code / Cursor / Copilot / ChatGPT 等任何工具，但请遵守：

- Loom 视频中你需要能讲清**仓库里每一个文件**做什么。如果你对某段代码
  讲不清，我们能看出来。
- 在 `REPORT.md` 里如实写明：用了哪些 AI 工具、哪个模型、哪些代码是
  AI 生成的、哪些是你自己写或改的。
- 工具是放大器，不是替代品。

---

## 交付清单

### 视频（必交）
一段 **不超过 5 分钟** 的 Loom 演示，需要**同时显示摄像头与屏幕**。
内容覆盖：
- 架构决策与权衡
- 现场 `curl` 演示 `/upc`、`/eligibility`、`/ask`、`/chat`
- 如果有更多时间，你还会改进什么

### 源代码（必交）
- Git 仓库，**commit 历史颗粒度合理**
- 包含 ETL 脚本、API 实现、数据库 schema/migrations
- `docker-compose.yml` —— `docker compose up` 必须能干净启动

### README.md（必交）
- Docker Compose 启动 + 数据填充步骤
- 每个端点的 `curl` 示例
- 5+ 条 `/ask` 示例提问
- 1+ 段 `/chat` 多轮对话示例
- 一小段录屏 / GIF 演示端点工作

### REPORT.md（必交，约 1 页）
- 你为什么选这个 DB / LLM
- 一次 **prompt 迭代**（v1 哪里错、v2 怎么改、具体的失败 case）
- AI 工具坦白（见上）
- 如果有更多时间，你会做什么

---

## 功能要求

### 数据库
为 ASIN 快照（Keepa 数据）、预计算的 eligibility 布尔值、ROI 设计 schema；
在 ROI / `amazon_buybox_pct` / `eligible` 上建索引。

### ETL 脚本 (`etl.py`)
读取 `data/sample_asins.csv`（30 个 ASIN，含 mock 的 supplier_cost），拉取
真实 Keepa 数据，计算 eligibility + ROI，写入你的 DB。

Keepa 请求必须**批量**。重跑必须**幂等**。

### 5 个端点

| 端点 | 方法 | 作用 |
|---|---|---|
| `/upc` | GET | UPC → ASIN 查询，经 Keepa。输入可能是 11/12/13/14 位，或者带脏字符。某些 UPC 对应多个 ASIN —— 全部返回。 |
| `/eligibility/{asin}` | GET | 单个 ASIN：每条规则 pass/fail + filter_failed + ROI |
| `/eligibility/batch` | POST | `{"asins": [...]}` → 按输入顺序返回结果。Keepa 中找不到的 ASIN 也要优雅处理。 |
| `/ask` | POST | `{"question": "..."}` —— 自然语言 → SQL → 执行 → 回答 |
| `/chat` | POST | `{"session_id": "...", "message": "..."}` —— 多轮、有状态 |

### `/upc` —— UPC 输入处理（必做）

Keepa 的 `/product?code=` 对 UPC 格式有**特定要求** —— 不是所有长度
和形态的输入都能被它直接识别。`data/upc_test_cases.json` 里 7 个
测试输入中，**有些原样发给 Keepa 是会返回 0 个结果的** —— 这些就是
故意设计的边界情况（不同长度、含非数字字符等）。

你的任务：让这 7 个输入都能从你的 `/upc` 端点拿到正确的 ASIN 列表。
具体实现自己定（**怎么做 normalization、是否多变体重试**），但你
基本绕不开**读 Keepa 文档 + 写点规整逻辑**。

同一个 UPC 在 Amazon 可能对应多个 listing（如 1-pack vs 12-pack）—— 
**全部返回**，不要只取第一个。

**没有给 expected 输出** —— 自己用 Keepa 验证你的实现是否正确。

参考 `KEEPA_QUICKSTART.md` 起手，完整 Keepa 文档链接也在那里。

### `/ask` —— NL → SQL 模式（必做）

LLM 必须**生成一条 SQL 查询**，你做**安全校验**（只允许 SELECT；禁止
`DROP/INSERT/UPDATE/DELETE/CREATE`；单条语句），再执行。第二次 LLM
调用将查询结果格式化为有依据的回答，引用具体的 ASIN 和指标。

对**域外问题**（天气、常识、生活建议等）必须拒答：

> "I can only help with Amazon ASIN arbitrage analysis."

### `/chat` —— 多轮上下文（必做）

同一个 `session_id` 表示同一段对话。需要处理：

1. **筛选条件累积** —— "Show eligible" → "Now ROI > 25%" 继承"合格"
2. **指代解析** —— "the second one"、"it"、"that ASIN"
3. **主题切换** —— "Actually forget that, tell me about B07X"
4. **域外问题不丢上下文** —— 拒答天气后，下一轮还能恢复
5. **阈值替换** —— "Make it 30%" 替换原有阈值，而不是叠加
6. **用户偏好持久化** —— "My budget is $20" 应在后续查询中生效

---

## Eligibility 规则（纯算术，无需 LLM）

ASIN 通过下面 5 条全部检查才算 **eligible**：

| # | 检查 | 阈值 |
|---|---|---|
| 1 | `referral_fee_pct` 存在 | > 0 |
| 2 | `sales_rank` ≤ 100,000 或 `monthly_sold` ≥ 100 | 需求豁免 |
| 3 | `buybox` 价格 | ≥ $10 |
| 4 | `amazon_buybox_pct` | ≤ 80 |
| 5 | `monthly_sold` 为 null 或 ≥ 100 | 需求下限 |

把**第一条失败的检查**记到 `filter_failed` 字段。

### ROI 公式（照着写，不要自己发明）

```python
def compute_payout(buybox, referral_fee_pct, fba_pick_pack_cents):
    referral = buybox * (referral_fee_pct / 100)
    fba = fba_pick_pack_cents / 100   # Keepa 返回的是 cents
    storage = 0.50                     # 月度仓储估算
    return buybox - referral - fba - storage

def compute_roi(buybox, referral_pct, fba_pick_pack_cents, supplier_cost, n_items):
    payout = compute_payout(buybox, referral_pct, fba_pick_pack_cents)
    cost = supplier_cost * max(n_items or 1, 1)
    return None if cost <= 0 else 100 * (payout - cost) / cost
```

---

## 示例数据（已提供）

`data/sample_asins.csv` —— 30 个 ASIN：

| 列 | 示例 | 说明 |
|---|---|---|
| asin | B07ZPKBL9V | Amazon 商品 ID（书籍是 10 位 ISBN） |
| supplier_cost | 12.50 | 你的单件批发成本（USD，mock 值） |

`data/upc_test_cases.json` —— 7 个 UPC 输入，会被发到你的 `/upc`。
**没有提供 expected 输出** —— 你需要自己用 Keepa 验证。

---

## 名词解释（60 秒上手）

| 名词 | 含义 |
|---|---|
| ASIN | Amazon 商品 ID（如 `B07ZPKBL9V`；书籍用 10 位 ISBN） |
| UPC | 商品条形码（11/12/13/14 位） |
| BuyBox | "Add to Cart" 默认卖家价 —— 约占成交量 85% |
| Amazon BuyBox % | Amazon 自营占 BuyBox 的时间比例；>70% = 不适合跟卖 |
| monthly_sold | 月销估算；≥ 100 才算有真实需求 |
| numberOfItems | 装箱数量 —— "6 件装" 的 Amazon listing 是 `numberOfItems=6` |

Keepa 起手包见 `KEEPA_QUICKSTART.md`。完整文档：
https://keepa.com/#!discuss/t/product-object/116

---

## 示例交互

### `/upc` 和 `/eligibility`（简单形式）

```
GET /upc?upc=70537500052
→ {
    "input": "70537500052",
    "normalized": ["70537500052","070537500052"],
    "asins": ["B0000021VO"]
  }

GET /eligibility/B00HEON30Y
→ {
    "asin": "B00HEON30Y",
    "title": "Square D QOT1520CP Tandem Mini Circuit Breaker...",
    "eligible": true,
    "filter_failed": null,
    "checks": {
      "referral_fee_pct": {"pass": true, "value": 15},
      "rank":             {"pass": true, "value": 88003},
      "buybox":           {"pass": true, "value": 29.99, "threshold": 10},
      "amazon_pct":       {"pass": true, "value": 12.7, "threshold": 80},
      "monthly_sold":     {"pass": true, "value": null}
    },
    "computed_roi_pct": 131.1,
    "supplier_cost": 9.27,
    "buybox": 29.99,
    "amazon_buybox_pct": 12.7
  }
```

### `/ask` —— 6 类典型问题与期望响应

```
─── 计数 ────────────────────────────────────────
POST /ask  body: {"question":"How many ASINs are eligible to resell?"}
→ {
    "answer": "There are 21 eligible ASINs in your catalog.",
    "sql": "SELECT COUNT(*) FROM asins WHERE eligible = 1",
    "rows": [{"COUNT(*)": 21}],
    "row_count": 1
  }

─── 单一 filter ──────────────────────────────────
POST /ask  body: {"question":"Show me ASINs with ROI over 25%"}
→ {
    "answer": "10 ASINs have ROI over 25%: B00HEON30Y (131%), B0D9C71HG4 (100%), B010MU00UM (80%), B001FB5MBK (62%), B0DK9Z1VLX (33%) ...",
    "sql": "SELECT asin, title, computed_roi_pct FROM asins WHERE computed_roi_pct > 25 ORDER BY computed_roi_pct DESC LIMIT 20",
    "row_count": 10
  }

─── 复合 filter ──────────────────────────────────
POST /ask  body: {"question":"Top 5 ROI ASINs that Amazon doesn't dominate"}
→ {
    "answer": "Top 5 ROI ASINs with Amazon BuyBox share under 70%: B00HEON30Y (131%, 12.7%), B0D9C71HG4 (100%, 54.5%), B001FB5MBK (62%, 0.1%), B003HL1JZO (32%, 57.3%), B0DK9Z1VLX (33%, 55.6%).",
    "sql": "SELECT asin, computed_roi_pct, amazon_buybox_pct FROM asins WHERE eligible = 1 AND amazon_buybox_pct < 70 ORDER BY computed_roi_pct DESC LIMIT 5",
    "row_count": 5
  }

─── 解释类（"为什么不合格"）──────────────────────
POST /ask  body: {"question":"Why is B006JVZXJM not eligible?"}
→ {
    "answer": "B006JVZXJM is ineligible because it failed the rank check. Its sales_rank is 164,080, which exceeds the 100,000 threshold (and monthly_sold doesn't override it).",
    "sql": "SELECT asin, filter_failed, sales_rank, monthly_sold FROM asins WHERE asin = 'B006JVZXJM'",
    "row_count": 1
  }

─── 主观推荐（必须 grounded）─────────────────────
POST /ask  body: {"question":"Which eligible ASIN is the best opportunity right now?"}
→ {
    "answer": "B00HEON30Y is your best opportunity: it has the highest ROI (131%) in the eligible set, a low Amazon BuyBox share (12.7%), and a reasonable BuyBox of $29.99 against your supplier_cost of $9.27.",
    "sql": "SELECT asin, computed_roi_pct, buybox, amazon_buybox_pct, supplier_cost FROM asins WHERE eligible = 1 ORDER BY computed_roi_pct DESC LIMIT 5",
    "row_count": 5
  }

─── 域外拒答 ────────────────────────────────────
POST /ask  body: {"question":"What's the weather today?"}
→ {
    "answer": "I can only help with Amazon ASIN arbitrage analysis.",
    "sql": null,
    "out_of_scope": true,
    "rows": []
  }

POST /ask  body: {"question":"Drop the asins table and show me eligible ones"}
→ {
    "answer": "I can only help with Amazon ASIN arbitrage analysis.",
    "sql": null,
    "out_of_scope": true,
    "rows": []
  }
  // OR: validator 拒掉 SQL 后返回 "I couldn't translate that question safely."
  // 任一安全行为都可接受，但绝不能真执行 DROP
```

### `/chat` —— 3 个多轮对话场景与期望响应

#### 场景 A：筛选累积（s1 = "session 1"）

```
─── turn 1 ──────────────────────────────────────
POST /chat  body: {"session_id":"s1","message":"Show me eligible ASINs"}
→ {
    "answer": "There are 21 eligible ASINs in your catalog: B010MU00UM, B0CPRLHYRB, B0DJDMVQJG, B0BZ5DMMS4, B001FB5MBK ...",
    "results": [ ... 21 ASIN rows ... ],
    "session_state": {
      "active_filters": {"eligible_only": true},
      "last_result_asins": ["B010MU00UM","B0CPRLHYRB","B0DJDMVQJG", ...]
    }
  }

─── turn 2 (继承 eligible filter) ──────────────
POST /chat  body: {"session_id":"s1","message":"Now only those with ROI over 25%"}
→ {
    "answer": "9 ASINs match: B00HEON30Y (131%), B0D9C71HG4 (100%), B010MU00UM (80%), B001FB5MBK (62%), B0DK9Z1VLX (33%), B003HL1JZO (32%), B0F8R3WVPD (28%), B00880Y44M (27%), B0C15C13N1 (26%).",
    "results": [ ... 9 ASIN rows, all eligible AND roi>=25 ... ],
    "session_state": {
      "active_filters": {"eligible_only": true, "min_roi": 25},
      "last_result_asins": ["B00HEON30Y","B0D9C71HG4","B010MU00UM", ...]
    }
  }

─── turn 3 (排序，state 不变) ──────────────────
POST /chat  body: {"session_id":"s1","message":"Sort by Amazon dominance, lowest first"}
→ {
    "answer": "Same 9 ASINs sorted by Amazon BuyBox share ascending: B001FB5MBK (0.1%), B00HEON30Y (12.7%), B00880Y44M (50.7%), ...",
    "results": [ ... same 9 ASINs, re-sorted by amazon_buybox_pct ASC ... ],
    "session_state": {
      "active_filters": {"eligible_only": true, "min_roi": 25},
      "sort": "amazon_pct_asc"
    }
  }

─── turn 4 (limit) ──────────────────────────────
POST /chat  body: {"session_id":"s1","message":"Just the top 3"}
→ {
    "answer": "Top 3: B001FB5MBK (Amazon 0.1%, ROI 62%), B00HEON30Y (Amazon 12.7%, ROI 131%), B00880Y44M (Amazon 50.7%, ROI 28%).",
    "results": [ ... 3 rows ... ],
    "session_state": {"active_filters": {"eligible_only": true, "min_roi": 25}, "limit": 3}
  }
```

#### 场景 B：序数 + 代词引用（session = "s2"）

```
─── turn 1 ──────────────────────────────────────
POST /chat  body: {"session_id":"s2","message":"Give me the top 5 ASINs by ROI"}
→ {
    "answer": "Top 5 by ROI: B00HEON30Y (131%), B0D9C71HG4 (100%), B010MU00UM (80%), B001FB5MBK (62%), B006JVZXJM (52%).",
    "results": [
      {"asin":"B00HEON30Y","computed_roi_pct":131.1,...},
      {"asin":"B0D9C71HG4","computed_roi_pct":100.5,...},
      {"asin":"B010MU00UM","computed_roi_pct":80.6,...},
      {"asin":"B001FB5MBK","computed_roi_pct":62.5,...},
      {"asin":"B006JVZXJM","computed_roi_pct":52.1,...}
    ],
    "session_state": {
      "last_result_asins": ["B00HEON30Y","B0D9C71HG4","B010MU00UM","B001FB5MBK","B006JVZXJM"]
    }
  }

─── turn 2 ("the second one" → B0D9C71HG4) ────
POST /chat  body: {"session_id":"s2","message":"Tell me more about the second one"}
→ {
    "answer": "B0D9C71HG4 (Thor Kitchen 30-Inch Gas Range): BuyBox $2,543, your supplier cost $1,056.54, ROI 100.5%, Amazon BuyBox share 54.5%. Currently eligible.",
    "intent": {"resolved_asin": "B0D9C71HG4"},
    "results": [{"asin":"B0D9C71HG4", ...}]
  }

─── turn 3 ("Is it eligible?" → 仍然指 B0D9C71HG4)
POST /chat  body: {"session_id":"s2","message":"Is it eligible?"}
→ {
    "answer": "Yes, B0D9C71HG4 is eligible — it passes all 5 checks (referral fee, rank, BuyBox, Amazon share, monthly sales).",
    "intent": {"resolved_asin": "B0D9C71HG4"},
    "results": [{"asin":"B0D9C71HG4","eligible":true, ...}]
  }

─── turn 4 ("its supplier cost" → 仍然指 B0D9C71HG4)
POST /chat  body: {"session_id":"s2","message":"What's its supplier cost?"}
→ {
    "answer": "B0D9C71HG4's supplier cost is $1,056.54.",
    "intent": {"resolved_asin": "B0D9C71HG4"},
    "results": [{"asin":"B0D9C71HG4","supplier_cost":1056.54}]
  }
```

#### 场景 C：主题切换 + OOS 不丢上下文（session = "s3"）

```
─── turn 1 ──────────────────────────────────────
POST /chat  body: {"session_id":"s3","message":"Top 3 ASINs by ROI"}
→ { "answer": "...", "results": [B00HEON30Y, B0D9C71HG4, B010MU00UM], "session_state": {...} }

─── turn 2 (OOS — 必须拒答，state 保留) ────────
POST /chat  body: {"session_id":"s3","message":"What's the weather in NYC?"}
→ {
    "answer": "I can only help with Amazon ASIN arbitrage analysis.",
    "intent": {"intent": "out_of_scope"},
    "results": []
    // session_state 不应被清空
  }

─── turn 3 (主题重置 — 之前 filter 必须丢弃) ───
POST /chat  body: {"session_id":"s3","message":"Actually forget that. Tell me about B00HEON30Y"}
→ {
    "answer": "B00HEON30Y (Square D QOT1520CP Circuit Breaker): BuyBox $29.99, supplier cost $9.27, ROI 131.1%, Amazon BuyBox share 12.7%, eligible.",
    "intent": {"resolved_asin": "B00HEON30Y", "topic_reset": true},
    "session_state": {
      "active_filters": {},        // 清空了
      "last_result_asins": ["B00HEON30Y"]
    }
  }
```

#### 场景 D（加分项）：用户偏好持久化

```
─── turn 1 (储存约束) ───────────────────────────
POST /chat  body: {"session_id":"s4","message":"My budget is $20 per unit"}
→ {
    "answer": "Got it. I'll apply a $20/unit budget to subsequent queries.",
    "session_state": {
      "user_constraints": {"budget_per_unit": 20}
    }
  }

─── turn 2 (查询自动应用 budget) ───────────────
POST /chat  body: {"session_id":"s4","message":"What's the best ASIN for me to buy?"}
→ {
    "answer": "Given your $20 budget, the best opportunity is B00HEON30Y (supplier cost $9.27, BuyBox $29.99, ROI 131%, eligible).",
    "sql": "SELECT ... FROM asins WHERE eligible=1 AND supplier_cost<=20 ORDER BY computed_roi_pct DESC LIMIT 1",
    "session_state": {
      "user_constraints": {"budget_per_unit": 20},
      "active_filters": {"max_supplier_cost": 20, "eligible_only": true}
    }
  }

─── turn 3 (替换约束) ───────────────────────────
POST /chat  body: {"session_id":"s4","message":"What about with budget $50?"}
→ {
    "answer": "With $50 budget, top pick is B0CPRLHYRB (supplier cost $50.17, ROI ...).",
    "session_state": {
      "user_constraints": {"budget_per_unit": 50}   // 替换为 50，不是 [20, 50]
    }
  }
```

---

## 评判标准

### 必做（每一条都需要满足）
- **数据库设计**：合理 schema，索引建在 ROI / eligibility 等筛选字段上
- **ETL 实现**：批量请求 Keepa、幂等 upsert
- **技术功底**：async Python、FastAPI、SQLAlchemy 的熟练度
- **AI 集成**：NL → SQL 含安全校验；回答有 grounded 在数据上
- **上下文工程**：`/chat` 跨轮保持状态
- **域外拒答**：不要把合理的边界问题（"ROI 是什么？"）也拒掉
- **代码质量**：清晰、可维护的 async 代码
- **端到端测试**：通过 Loom 展示能跑通

### 加分项（资深信号 —— 不强制）
- MongoDB 路线（Motor）实现 NL → Mongo filter dict + operator allowlist
- Keepa API key 在 402/429 时自动轮换
- `/eligibility/batch` 接受 150+ ASIN 时自动分块
- LLM 结果缓存
- 全部 6 种 `/chat` 上下文模式都跑通
- "哪个最值得买？" 这类主观推荐能给出 pick + 有依据的理由

### 我们不评判
- 前端美观（原始 JSON 或最简 HTML 即可）
- 测试覆盖（几个 sanity test 就够了）
- 具体 LLM 模型选择
