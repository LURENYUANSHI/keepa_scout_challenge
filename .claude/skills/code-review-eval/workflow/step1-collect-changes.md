# Step 1 - 收集本次变更

## 目标

获得一份完整、结构化的"本次提交将包含什么"清单，后续所有检查都基于它。

## 操作

1. `git status --short` —— 总览
2. `git diff HEAD` —— 完整 diff（已暂存 + 未暂存）
3. `git ls-files --others --exclude-standard` —— 未跟踪新文件，逐个 Read 全文
4. 若已经 `git add` 过：同时看 `git diff --cached`

## 输出结构

整理出一份清单（在思考中维护，不需要写入文件）：

```
文件: path/to/file.py
  变更类型: modified | added | deleted | renamed
  行数: +15 / -3
  函数级变更:
    - 新增: foo(a, b)
    - 修改: bar(x)  —— 变更摘要
  非函数变更: import 调整 / 常量调整 / 配置 / ...
```

同时记录：
- 本次修改触及的**模块/包**（用于 Step 3 反向依赖搜索）
- 本次修改涉及的**公共 API 导出**（用于 Step 5 breaking change 判断）

## 原始任务意图

在继续之前，明确一句话回答："**本次改动是为了完成什么任务？**" 这个意图是 Step 2 最小化检查的基准。如果自己都说不清，那本次改动一定超范围了。
