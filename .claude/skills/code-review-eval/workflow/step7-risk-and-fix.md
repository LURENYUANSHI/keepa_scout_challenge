# Step 7 - 汇总风险等级并决策

## 汇总规则

收集 Step 2-6 的各子项风险，取**最高值**作为总风险：

```
total_risk = max(
  minimality_risk,
  side_effect_risk,
  quality_risk,
  breaking_risk,
  coverage_risk
)
```

风险等级定义见 [../rules/risk-levels.md](../rules/risk-levels.md)。

## 决策

### total_risk == Low

✅ 允许进入 `git commit`。

输出一条简报给用户：
```
[code-review-eval] Risk: Low
- files reviewed: N
- no blocking issues
Proceeding to commit.
```

### total_risk == Medium / High / Critical

❌ **不允许 commit**。立即进入修复流程：

1. **按子项发现逐项修复**（下方"修复手册"）
2. 修复完成后，**重新触发 auto-test-writer**（代码改了，测试可能受影响）
3. auto-test-writer 通过后，**从 Step 1 重跑整个 code-review-eval**
4. 如此循环直到 total_risk == Low

**禁止**：
- 跳过修复直接 commit
- 只局部重评（必须从 Step 1 全跑）
- 通过放宽评估标准让风险"看起来"变 Low

### 连续 3 轮仍未降到 Low

停下来向用户汇报当前 findings 和已尝试的修复，请求决策。不要无限循环。

## 修复手册

| 子项 | 典型修复动作 |
| ---- | ------------ |
| minimality | 用 Edit 回退多余改动到原始行 |
| side_effects | 修源码使调用方仍然工作；或加兼容 wrapper |
| quality.hardcode | 提取为模块级常量 / 读取配置 |
| quality.duplication | 抽公共函数，替换所有调用点 |
| quality.naming | 改名（注意同步改所有引用） |
| quality.error_handling | 添加具体 except 分支 + 有意义的错误消息；绝不 `except: pass` |
| breaking | 恢复旧签名 / 加默认值 / 加 shim / deprecate 而非删除 |
| coverage | 调用 auto-test-writer 补测试用例 |

## 最终输出

```
[code-review-eval] Risk: <LEVEL>  (round N)
  minimality:    <level>  issues=<count>
  side_effects:  <level>  issues=<count>
  quality:       <level>  issues=<count>
  breaking:      <level>  issues=<count>
  coverage:      <level>  issues=<count>
decision: <COMMIT_ALLOWED | FIX_AND_RERUN | ESCALATE_TO_USER>
```
