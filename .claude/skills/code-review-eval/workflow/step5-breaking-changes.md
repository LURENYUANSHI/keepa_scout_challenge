# Step 5 - 破坏性变更检查

## 检查项

对本次变更，判断是否存在以下 breaking change：

### 5.1 公共 API 签名变化
- 函数 / 方法 / 类的参数：新增必填参数、删除参数、重命名参数、改变参数顺序、改变类型
- 返回值类型变化
- 异常类型变化（抛出的异常类从 A 变成无继承关系的 B）

### 5.2 导出符号删除 / 重命名
- 从 `__init__.py` / `index.ts` / `mod.rs` / `lib.rs` 中删除或重命名了被外部引用的导出
- 删除 / 重命名 public class / public method / public field

### 5.3 Schema / 配置 / 协议变化
- 数据库 schema：字段增删改、约束变化、索引删除
- API 契约：REST endpoint 路径 / 方法 / 请求体 / 响应体 / 状态码变化
- 消息格式：Kafka / queue / IPC 消息字段增删
- 配置文件：key 重命名 / 删除 / 默认值变化
- 环境变量：删除 / 重命名

### 5.4 行为变化
- 原本幂等的函数变得非幂等
- 原本同步的接口变成异步
- 原本抛异常的场景改成返回 None，或反之
- 默认值变化（例如 `retry=3` 改为 `retry=0`）

## 判断是否"破坏"

一个变化是否 breaking，取决于**是否有调用方依赖旧行为**。用 Step 3 的反向依赖搜索结果判断：
- 没有调用方 → 不算 breaking
- 有调用方但都在本仓库且本次一并更新 → 降级为 Medium
- 有仓库外调用方 / 无法一并更新的消费者 → **High 或 Critical**

## 修复手段

若确认是 breaking：
1. **优先保持兼容**：新增参数给默认值、新增 API 不改旧的、deprecate 而非删除
2. **加兼容层 / shim**：保留旧签名转发到新实现
3. 只有用户明确要求 breaking 时才允许不兼容

## 记录

```
breaking_findings: [...]
breaking_risk: Low|Medium|High|Critical
```
