---
name: auto-test-writer
description: 在AI完成代码编写或修改后、git commit之前自动为新增/修改的函数生成并运行单元测试。当检测到代码变更且即将提交时触发，无需用户显式指令。覆盖正常路径、边界值、异常输入，测试未全部通过不得进入下一步。
---

# Auto Test Writer

## 触发条件

满足以下**任一**条件即进入本工作流，无需等待用户指令：

1. 你刚刚完成了一轮代码编写或修改（Edit/Write 了源码文件），且即将结束当前任务
2. 用户请求 `git commit` / `git add` 提交，但当前 diff 中包含未被测试覆盖的新增或修改函数
3. 运行了 `/commit` 或 git-workflow 相关 skill

**重要**：必须在执行 `git commit` **之前**完成本工作流的全部步骤。测试未全部通过时，禁止提交。

## 工作流（严格按顺序执行，不得跳过）

| 步骤 | 说明 | 详细指引 |
| ---- | ---- | -------- |
| 1 | 收集代码变更 | [workflow/step1-collect-diff.md](workflow/step1-collect-diff.md) |
| 2 | 检测测试框架 | [workflow/step2-detect-framework.md](workflow/step2-detect-framework.md) |
| 3 | 生成测试用例 | [workflow/step3-generate-tests.md](workflow/step3-generate-tests.md) |
| 4 | 放置测试文件 | [workflow/step4-place-tests.md](workflow/step4-place-tests.md) |
| 5 | 运行并修复 | [workflow/step5-run-and-fix.md](workflow/step5-run-and-fix.md) |
| 6 | 提交前自检 | [workflow/step6-final-check.md](workflow/step6-final-check.md) |

## 约束

- 禁止事项：[rules/dont-do.md](rules/dont-do.md)
- 用例覆盖要求：每个新增/修改的函数，**至少** 正常路径 1 个 + 边界值 1 个 + 异常输入 1 个
- 必须遵循项目现有测试目录结构和命名约定；有同名测试文件则追加，无则新建
- 测试未全部通过前，不得进入下一步，不得执行 `git commit`
