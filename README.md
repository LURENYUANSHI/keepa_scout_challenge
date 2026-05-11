# Keepa Scout — 笔试题

本仓库包含一份 AI 全栈工程师岗位的笔试题。完整任务说明、交付要求、评分标准
与示例交互都在 `candidate_package/` 下。请先阅读：

> [`candidate_package/CHALLENGE.md`](candidate_package/CHALLENGE.md)

附加资料：

- [`candidate_package/KEEPA_QUICKSTART.md`](candidate_package/KEEPA_QUICKSTART.md) — Keepa API 起手包
- [`candidate_package/env.example`](candidate_package/env.example) — 环境变量模板（含已分配的 Keepa keys）
- [`candidate_package/Dockerfile.example`](candidate_package/Dockerfile.example) — 参考 Dockerfile
- [`candidate_package/docker-compose.example.yml`](candidate_package/docker-compose.example.yml) — 参考 docker-compose
- [`candidate_package/data/sample_asins.csv`](candidate_package/data/sample_asins.csv) — 30 个 ASIN，ETL 输入
- [`candidate_package/data/upc_test_cases.json`](candidate_package/data/upc_test_cases.json) — 7 个 UPC 测试输入

## 启动

```bash
# 1. 克隆本仓库
git clone <本仓库 URL> && cd keepa_scout_challenge

# 2. 在你自己的工作目录里准备 .env（Keepa key 已经在里面）
cp candidate_package/env.example .env
# 编辑 .env，按需填入你的 LLM key

# 3. 写你的实现。参考 Dockerfile.example / docker-compose.example.yml，
#    但请按你的实现改名为 Dockerfile / docker-compose.yml。

# 4. 起服务
docker compose up --build
```

LLM 服务商任选 —— 海外（OpenAI / Anthropic / Gemini）或国内（DeepSeek /
Moonshot Kimi / 通义千问 Qwen / 智谱 GLM / 豆包 / Yi）都可以。

提交窗口：**72 小时**。

## 提交方式

回复原邮件，附上：

1. **Git 仓库 URL**（公开 GitHub / GitLab，commit 历史颗粒度合理）
2. **Loom 视频链接** —— ≤ 5 分钟，必须同时有摄像头画面 + 屏幕录制
3. 简要说明你完成了哪些部分，以及如果有更多时间还会做什么

## Docker

**必须项**：你的提交需要能用 `docker compose up --build` 一键起来，对外暴露
`:8000` 上的 5 个 endpoint。

- `Dockerfile.example` 和 `docker-compose.example.yml` 是参考起点
- 这两个文件是 SQLite 路线的最小版本；如果你用 Postgres 自己加 `db` 服务
- 你的 `.env` 文件要被 `docker-compose.yml` 读到（参考 example 里的 `env_file:`）
- ETL 应该在容器启动时自动跑（参考 example 里 CMD 的 `python -m app.etl &&` 部分）

## CI（可选，不强制）

不要求做 CI。但如果你愿意加一个 GitHub Actions 工作流验证 `docker compose up`
能 boot + 每个端点能响应，**算加分项**（说明你有自动化意识）。

如果做：

- 放在 `.github/workflows/`（仓库根目录下）
- secrets 在 GitHub Settings → Secrets and variables → Actions 里配 
- 跑 `docker compose up -d`，curl 每个端点验证返回 2xx
- 不用真打 Keepa（可以用 mock 或者直接验证 /health 上来就行）

我们不会因为你**没做** CI 而扣分，但做了会让我们对你的工程习惯印象更好。

## 时间预算

整个任务设计为在 **≤ 4 小时** 内完成（可以使用 Claude Code / Cursor /
Copilot 等 AI 辅助工具）。如果 4 小时内做不完也没关系，提交已完成的部分，
在 `REPORT.md` 中说明剩下的计划即可。

## 关于 AI 工具

允许使用 Claude Code / Cursor / Copilot / ChatGPT 等任何 AI 工具。但请注意：

- Loom 视频中你需要能讲清楚仓库里**每一个文件**做什么。如果你对某段代码
  讲不清，我们能看出来。
- 在 `REPORT.md` 中如实写明：用了哪些 AI 工具、哪个模型、哪些代码是你
  让 AI 生成的、哪些是你自己写或改的。

我们希望招的是能驾驭这些工具、并对自己的工程判断负责的人。
