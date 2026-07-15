# Step 6 - 测试覆盖检查

## 目标

确认本次新增 / 修改的代码行**都被测试执行到**。

## 操作

### 有 coverage 工具的情况

优先使用项目已有的 coverage 工具，只针对变更文件跑：

- Python: `pytest --cov=<module> --cov-report=term-missing <test_files>`
- JS/TS (vitest): `npx vitest run --coverage`
- JS/TS (jest): `npx jest --coverage --collectCoverageFrom='<changed files>'`
- Go: `go test -cover -coverprofile=/tmp/c.out ./... && go tool cover -func=/tmp/c.out`

读取输出，找到本次变更文件的**未覆盖行号**。

### 无 coverage 工具的情况

手动比对：
1. 列出本次变更的函数
2. 对每个函数，Grep 测试目录找对应的 test 文件
3. 若找不到任何 test 引用该函数 → 未覆盖
4. 若有 test 但只覆盖部分分支，人工判断

## 判断标准

- 每个新增/修改的**函数**至少有一个 test 用到它 —— 这应由 auto-test-writer 保证，若失败说明 auto-test-writer 出了 bug
- 每个新增/修改的**分支**（if/else、try/except）应至少被一条 test case 执行到
- **纯配置 / 常量定义文件** 不强制要求覆盖

## 发现未覆盖时

- 未覆盖的是新代码 → **High**，回到 auto-test-writer 补测试
- 未覆盖的是修改的旧代码分支 → **Medium**
- 未覆盖的是防御性代码（理论不可达） → **Low**，但需加注释说明为什么不测

## 记录

```
coverage_findings: [{file, line, function, reason}]
coverage_risk: Low|Medium|High
```
