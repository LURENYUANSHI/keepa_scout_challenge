# Step 4 - 放置测试文件

## 决策：追加还是新建？

对每个被测源文件 `src/foo/bar.py`：

1. 按项目命名约定推断目标测试路径，例如：
   - pytest: `tests/foo/test_bar.py` 或 `tests/test_bar.py`
   - vitest/jest: `src/foo/bar.test.ts` 或 `__tests__/bar.test.ts`
   - go: 同目录 `bar_test.go`
2. 使用 Glob 确认该路径是否已存在
3. **存在** → Read 后用 Edit **追加**新的测试函数，保持文件已有 import / fixture 不重复
4. **不存在** → 用 Write 新建，完整包含 import、fixture、所有新 case

## 规则

- 不得删除或改写现有测试，除非它因源码变更而必然失败（此时在 Step 5 处理）
- 新建目录需和项目现有结构一致：如项目所有测试都在 `tests/`，就不要在源码旁新建 `__tests__/`
- 保持一个源文件 ↔ 一个测试文件的映射，不要把多个模块的测试堆在一起
- 新增 import 只引入确实用到的模块
