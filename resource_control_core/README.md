# Resource Control Core

Campus GPU Fabric 的独立资源控制面模块。它不包含、也不修改任何前端代码；`core` 是 Node、GPU、Allocation、Job、租约、抢占和资源账本的唯一权威。

## 运行

```bash
cd resource_control_core
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

默认监听 `http://localhost:8081`，Swagger 位于 `/docs`。

默认使用 SQLite（`resource_control.db`）。生产环境切换 PostgreSQL：

```bash
export DATABASE_URL='postgresql+psycopg://user:password@localhost:5432/gpu_fabric'
export TEST_KEY='replace-me'
export ADMIN_KEY='replace-me'
python main.py
```

## 设计保证

- 所有资源检查、GPU 锁定、Allocation 更新和事件写入位于同一数据库事务。
- PostgreSQL 使用 `SELECT ... FOR UPDATE` 锁定资源；SQLite 作为单实例演示和测试后端。
- 多 GPU 必须在同一 Node 上一次性满足，否则整个请求进入 `Pending`，不会部分占卡。
- CPU、内存、临时磁盘和 GPU 同时参与容量检查。
- 终态 `Released / Expired / Evicted / Rejected` 不占资源。
- `Owner + operation + Idempotency-Key` 唯一；同 Key 不同请求返回 409。
- Owner 越权读取返回 404，避免泄露其他租户资源 ID。
- 租约、排队超时、Checkpoint timeout、回收 Deadline、贡献者归还和 Job timeout 全由逻辑时钟驱动。
- `Draining` 禁止新放置但不杀现有任务；`NotReady` 与 GPU Offline 会 Evict 并清理账本。
- 私有测试控制接口由 `X-Test-Key` 保护；内部管理接口由 `X-Admin-Key` 保护。
- `/internal/invariants` 可检查 GPU 双重占用、账本不一致和幽灵 Holder。

## 状态闭环

```text
Pending -> Allocated -> Running
Pending -> Rejected                    # queue timeout
Allocated/Running -> Released          # owner release
Allocated/Running -> Expired           # TTL
Allocated/Running -> PreemptRequested  # contributor demand
PreemptRequested -> Preempted          # reclaim deadline
Preempted/Evicted -> Allocated          # resume success
Preempted/Evicted -> Pending            # no replacement capacity
Allocated/Running/PreemptRequested -> Evicted  # node/GPU fault
```

Checkpoint `ready / failed / timeout` 只决定恢复质量，不阻止贡献者在 Deadline 收回 GPU。Resume 受 `max_restarts` 限制，无兼容资源时返回 202。

## 测试

```bash
pytest -q
```

覆盖多 GPU 原子分配、超卖防护、幂等冲突、Owner 隔离、4xx 无副作用、TTL/queue timeout、抢占与恢复、Checkpoint timeout、Node/GPU 故障、管理员挂载节点和故障注入。

## 文件结构

```text
core/api.py       FastAPI 路由、鉴权头、错误语义和故障注入
core/service.py   状态机、调度、账本、逻辑时钟、事件和不变量
core/models.py    SQLAlchemy 持久化模型
core/schemas.py   Pydantic 请求 Schema
core/db.py        SQLite/PostgreSQL 数据库会话
resources.json    确定性初始资源池
tests/            核心正确性测试
```
