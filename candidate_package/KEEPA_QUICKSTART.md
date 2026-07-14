# Keepa API 起手包

> 让你能跑起来的最小信息。完整文档：https://keepa.com/#!discuss/t/product-object/116
> 有些细节（请求成本公式、批量上限、CSV 数组下标含义）**故意没列出来**
> —— 你需要自己读文档或试探 API 摸清楚。

## 鉴权

所有端点用 `key` 这个 query 参数：

```bash
curl "https://api.keepa.com/<endpoint>?key=$KEEPA_API_KEY&domain=1&..."
```

`domain=1` = 美国市场（本笔试只用美国）。

## 你会用到的端点

### `/token`（免费 —— 不消耗 token）

查询当前余额和回血速率。

```bash
curl --compressed "https://api.keepa.com/token?key=$KEY"
# → {"tokensLeft": 300, "refillRate": 5, "refillIn": 35000, ...}
```

返回是 **gzip 压缩** —— curl 加 `--compressed`，或者用支持自动解压
的 HTTP 库。

### `/product`（成本视参数变化 —— 读文档）

按 ASIN 或 UPC/EAN code 拉商品数据。

```bash
# 按 ASIN（一个或多个 —— 批量上限自己看文档）：
curl "https://api.keepa.com/product?key=$KEY&domain=1&asin=B07XXX&stats=90&buybox=1&fbafees=1"

# 按 UPC/EAN code：
curl "https://api.keepa.com/product?key=$KEY&domain=1&code=034000681006"
```

**有用的参数**（每个都会加一点 token 成本 —— 读文档）：
- `stats=30` 或 `stats=90` —— 包含滚动统计
- `buybox=1` —— 包含当前 BuyBox 信息
- `fbafees=1` —— 包含 FBA referral + pick/pack 费用
- `stock=1` —— 包含当前库存估算

**你大概率会用到的响应字段**（按 product 维度）：
- `asin`、`title`、`brand`
- `numberOfItems`、`packageQuantity` —— 装箱数量
- `stats.current[]` —— 当前数值数组；**下标含义在 Keepa 文档里** ——
  你需要自己查清哪个下标对应 BuyBox / sales rank / new price
- `buyBoxSellerIdHistory` —— `[timestamp, sellerId, ...]` 交替排列
- `referralFeePercent`、`fbaFees.pickAndPackFee`

## 字段陷阱

- **所有价格都是 cents** —— 用之前要除以 100
- **`-1` 表示 "无数据"** —— 防御性处理，不要当 0
- 时间序列 `csv[]` 数组是 `[timestamp, value, timestamp, value, ...]` 交替排列
- Amazon 自营卖家 ID 是 `ATVPDKIKX0DER` —— 用它从 `buyBoxSellerIdHistory`
  计算 Amazon 占 BuyBox 的比例

## HTTP 状态码

| Code | 含义 |
|---|---|
| `200` | OK |
| `400` | 请求错误（比如一次发太多 ASIN） |
| `402` | 这个 key 的 token 用光了 |
| `429` | 限流 —— 退避后重试 |

## 你需要自己摸清的事

- 一次 `/product` 调用最多能塞多少 ASIN（试试就知道）
- Token 成本公式（追踪响应里的 `tokensConsumed`）
- `stats.current[]` 哪个下标是 BuyBox、哪个是 sales rank（读文档）
- 如何从 `buyBoxSellerIdHistory` 解码出 Amazon 的 BuyBox 占比
- 历史序列里的时间戳（keepaTime）怎么换算成真实时间（读文档，
  或者找找有没有现成的库）
- **UPC 哪些格式 Keepa 接受、哪些不接受** —— 拿 11/12/13/14 位的
  样例 UPC 各打一次试试，看哪些返回 0 个结果。返回 0 的就是需要
  在你这边预处理的

这些都是本笔试的一部分 —— 不要指望我们把这些直接告诉你。
