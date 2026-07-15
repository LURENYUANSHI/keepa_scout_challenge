---
name: code-review-eval
description: 在 auto-test-writer 通过之后、git commit 之前自动触发，作为 AI 提交代码前的最后一道关卡。对本次所有变更做全面 review（最小化、副作用、代码质量、破坏性变更、测试覆盖），给出风险等级 Low/Medium/High/Critical。风险 >= Medium 时直接修复后重评，循环直到 Low 才允许 commit。无需用户显式指令。
---

# Code Review Eval

## 触发条件

**必须**在以下时机触发，无需等待用户指令：

1. `auto-test-writer` 工作流的 Step 6（final-check）全部通过后
2. 即将执行 `git commit`（无论是直接调用 Bash 还是走 git-workflow / `/commit`）之前

**顺序硬约束**：`代码修改 → auto-test-writer → code-review-eval → git commit`。禁止在 code-review-eval 风险等级达到 **Low** 之前执行 commit。

## 工作流

| 步骤 | 说明 | 详细指引 |
| ---- | ---- | -------- |
| 0 | 前置检查（auto-test-writer 已通过） | [workflow/step0-precheck.md](workflow/step0-precheck.md) |
| 1 | 收集本次变更 | [workflow/step1-collect-changes.md](workflow/step1-collect-changes.md) |
| 2 | 最小化修改检查 | [workflow/step2-minimality.md](workflow/step2-minimality.md) |
| 3 | 副作用检查 | [workflow/step3-side-effects.md](workflow/step3-side-effects.md) |
| 4 | 代码质量检查 | [workflow/step4-quality.md](workflow/step4-quality.md) |
| 5 | 破坏性变更检查 | [workflow/step5-breaking-changes.md](workflow/step5-breaking-changes.md) |
| 6 | 测试覆盖检查 | [workflow/step6-coverage.md](workflow/step6-coverage.md) |
| 7 | 汇总风险等级并决策 | [workflow/step7-risk-and-fix.md](workflow/step7-risk-and-fix.md) |

## 项目专属检查命令

**本仓库使用的 lint / type-check / test / coverage 命令见** [config/project-checks.md](config/project-checks.md)。Step 3 和 Step 6 必须读这份配置，不要自己猜命令。

## 风险等级定义

见 [rules/risk-levels.md](rules/risk-levels.md)。

## 禁止事项

见 [rules/dont-do.md](rules/dont-do.md)。

## 核心硬约束

- 风险 >= **Medium** → 立即修复后**重新跑完整个 review 流程**（从 Step 1 开始），不得只局部重跑
- 修改代码后必须**重新触发 auto-test-writer**（修改可能影响测试）
- 只有最终评估为 **Low** 才允许 commit
- 禁止通过"降低评估标准"让风险看起来更低 —— 只能通过修代码来降风险
