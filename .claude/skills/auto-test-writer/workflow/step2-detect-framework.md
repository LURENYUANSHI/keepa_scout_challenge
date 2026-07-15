# Step 2 - 检测测试框架

## 目标

判断当前项目使用的测试框架，以及现有测试目录结构和命名约定。

## 检测优先级

按以下顺序检查项目文件，**第一个命中的即为准**：

### Python
- `pyproject.toml` 含 `[tool.pytest]` 或 `pytest` 依赖 → **pytest**
- 存在 `tests/` 目录且文件名为 `test_*.py` → **pytest**
- 否则 `unittest`

### JavaScript / TypeScript
- `package.json` 的 devDependencies 含 `vitest` → **vitest**
- 含 `jest` → **jest**
- 含 `mocha` → **mocha**
- 含 `@playwright/test` 且变更是 E2E 场景 → **playwright**

### Go
- 存在 `go.mod` → **go test**（`*_test.go` 同目录）

### Java
- `pom.xml` 或 `build.gradle` 含 junit → **JUnit 5**

### Rust
- `Cargo.toml` → `#[cfg(test)]` 内联 或 `tests/` 集成测试

## 记录

在思考中记录：

```
框架: pytest
测试根目录: tests/
命名约定: test_<module>.py
运行命令: pytest -q
```

## 找不到框架

- 如果项目完全没有测试基建：选择语言对应的主流默认（Python→pytest，JS/TS→vitest，Go→go test）
- 首次运行时允许新建测试目录，但不要擅自修改 `package.json` / `pyproject.toml` 添加新依赖 —— 若必须新增依赖，先停下来告知用户
