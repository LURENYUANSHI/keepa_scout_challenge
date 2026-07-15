# Step 0 - 前置检查

在开始 review 前确认：

1. `auto-test-writer` 工作流最近一次运行的 Step 6 全部通过（增量+全量测试绿灯）
2. 若未运行过 auto-test-writer：**先调用 auto-test-writer skill**，完成后再回到本工作流
3. 工作区当前状态与 auto-test-writer 通过时一致（没有在测试通过之后又乱改代码）

若任一条件不满足，不得进入 Step 1，先补齐再说。
