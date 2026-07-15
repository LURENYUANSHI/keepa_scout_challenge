# Step 1 - 收集代码变更

## 目标

找出当前工作区相对最近一次 commit 的所有新增和修改的源码，并提取函数/方法级别的变更清单。

## 操作

1. 运行 `git status --short` 查看改动文件总览
2. 运行 `git diff HEAD` 获取未提交的完整 diff（包含已 `git add` 和未 add 的）
3. 若存在未跟踪文件，运行 `git ls-files --others --exclude-standard` 并 Read 每个新文件全文
4. 过滤掉非源码文件：
   - 忽略：`*.md`、`*.json`（除非是源码）、锁文件、构建产物、`node_modules`、`dist`、`.venv` 等
   - 保留：实际的源码文件（`.py .js .ts .jsx .tsx .go .java .rs .rb` 等）

## 输出

在思考过程中整理出：

```
变更清单：
- path/to/file1.py
  - 新增函数: foo(a, b)
  - 修改函数: bar(x) —— 签名/逻辑变化描述
- path/to/file2.ts
  - 新增函数: baz()
```

**只纳入函数/方法级别的变更**。纯格式化、注释修改、import 调整不需要生成测试。

若变更清单为空，跳过后续步骤，直接结束本工作流。
