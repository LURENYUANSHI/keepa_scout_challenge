# Step 3 - 副作用检查

## 目标

**静态分析**所有依赖本次变更文件的模块，判断变更是否会破坏它们。

## 操作

对每个修改的源文件 / 修改的导出符号：

1. **反向依赖搜索**：用 Grep 搜索谁 import 了这个文件 / 这个符号
   - Python: `from path.to.module import foo` / `import path.to.module`
   - JS/TS: `from '../file'` / `require('../file')`
   - Go: `"module/path"`
2. 对每个调用点 Read 上下文（前后 20 行），判断：
   - 调用方式是否仍然合法（签名兼容？返回值类型兼容？）
   - 调用方假设的副作用 / 顺序 / 幂等性是否被改变
   - 抛出的异常类型是否改变，导致上游 catch 失效
3. 特别关注：
   - 被改动的函数若在 `__init__.py` / `index.ts` / `mod.rs` 中被 re-export
   - 被改动的常量 / 枚举值（调用方可能硬编码依赖旧值）
   - 被改动的数据库 schema / API 契约 / 消息格式
   - 共享的全局状态、单例、缓存

## 运行静态检查工具（如果项目有）

若项目配置了以下工具，跑一遍：
- `mypy` / `pyright` / `pyre`（Python）
- `tsc --noEmit`（TypeScript）
- `go vet ./...`（Go）
- `cargo check`（Rust）

只看**本次变更文件 + 其反向依赖文件**的新增错误，忽略历史遗留问题。

## 记录

```
side_effect_findings: [
  {file, caller, issue_description}
]
side_effect_risk: Low|Medium|High|Critical
```
