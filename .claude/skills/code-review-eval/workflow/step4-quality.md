# Step 4 - 代码质量检查

仅针对**本次新增/修改的代码行**，不要评审未触及的历史代码。

## 4.1 硬编码 / Magic Values

扫描变更行中的：

- **Magic number**：`timeout=30`、`retry=5`、`0.95` 等无上下文数字 —— 应提取为命名常量
- **硬编码 URL / 路径**：`http://...`、`/var/log/...`、`C:\...` —— 应来自配置 / 环境变量
- **硬编码密钥 / token / 密码**：任何疑似凭据的字符串 → **Critical**，立即移除
- **硬编码业务配置**：阈值、比例、时长、限额 —— 应来自配置文件或常量模块

**例外**：`0`/`1`/`-1`/`""`/空集合这类语义自明的值不需要提取。

## 4.2 重复代码

- 本次新增的两段代码之间是否有明显 copy-paste？
- 本次新增的代码是否与项目里已存在的函数做同一件事？（Grep 找现有实现）
- 2 次重复可以容忍；**3 次及以上必须抽函数**

## 4.3 命名规范

- 变量 / 函数命名是否表意清晰？避免 `data`、`info`、`tmp`、`result`、`handle` 等空洞名
- 是否符合项目既有风格？（snake_case / camelCase / PascalCase —— Read 1-2 个现有文件确认）
- 缩写是否会让后人迷惑？（`usr_cfg_mgr` 这种）
- 布尔变量 / 函数是否用 `is_` / `has_` / `should_` 前缀？

## 4.4 错误处理

- 是否有 `except: pass` / `except Exception: pass` / `catch {}` 吞掉异常？
- 是否有 `try/except` 但 except 分支里什么都不做或只打印？
- 外部调用（网络、DB、文件、子进程）是否完全没有错误处理？
- 抛出的异常是否过于宽泛（`raise Exception("...")`）？
- 错误消息是否包含足够的上下文（哪个参数、哪个文件）？

## 记录

```
quality_findings: [
  {category: hardcode|duplication|naming|error_handling, location, issue}
]
quality_risk: Low|Medium|High|Critical
```
