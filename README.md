# Campus GPU Fabric · Server

校园 GPU 算力互助平台的**服务端**，按冻结的 `api/openapi.yaml`（v2.0.0）实现。
当前进度：**第 1 步 · 资源模型与 capacity**。端点已全部存在；资源池来自 `resources.json`，
`/v1/capacity` 反映逻辑时间快照。账本、到期队列、抢占时序在后续步骤补齐。

## 启动

```bash
pip install -r requirements.txt
python run.py
```

- BASE_URL：`http://localhost:8080`
- OpenAPI 文档：`http://localhost:8080/docs`
- 单进程 Python 3.11+ / FastAPI，内存状态，无数据库、Redis、消息队列。
- 开发热重载：`RELOAD=1 python run.py`（默认关闭）。
- 契约原文件：[api/openapi.yaml](api/openapi.yaml)；赛题说明：[api/challenge.md](api/challenge.md)。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TEST_KEY` | `campus-test-key` | `/_test/*` 的 `X-Test-Key` |
| `RESOURCES_FILE` | 仓库根 `resources.json` | 初始资源池；文件格式见下 |

## 请求头约定

| 头 | 作用于 | 说明 |
| --- | --- | --- |
| `X-Owner` | `/v1/allocations*`、`/v1/jobs*`、`/v1/events`、`/v1/metrics` | 必填；缺失 400 `owner_required` |
| `X-Test-Key` | `/_test/*` | 错误返回 403 |
| `Idempotency-Key` | `POST /v1/allocations` | 最长 128 字符（第 1 步仅校验长度，去重在第 3 步） |

`/healthz`、`/v1/version`、`/v1/capacity` 不要求鉴权。错误体统一 `{error, detail, request_id}`，
响应头带 `X-Request-Id`。body `owner` 与 `X-Owner` 不一致 → 400，detail 写明两个值。
`renew` 的 `ttl_seconds` 省略（含无 body）时沿用当前值。

## 初始资源（`resources.json`）

| node_id | GPU | 数量 | 显存 | 标签 |
| --- | --- | --- | --- | --- |
| central-01 | H100 | 2 | 81559 MiB | `zone=campus-core, tier=public` |
| vision-01 | RTX 4090 | 2 | 24564 MiB | `zone=campus-lab, lab=vision` |
| nlp-01 | A100 | 2 | 40960 MiB | `zone=campus-lab, lab=nlp` |

上限：6 张 GPU、CPU 168000 millicores、内存 589824 MiB、临时存储 1152000 MiB。
`gpu_id` 显式写在文件里（如 `central-01-gpu-0`），跨 `/_test/reset` 稳定。

文件格式（B 写生成器用；校验失败会 fail-fast 并指出节点/字段）：

```json
{
  "version": 1,
  "nodes": [{
    "node_id": "central-01",
    "state": "Ready",
    "cpu_millicores": 64000,
    "memory_mib": 262144,
    "ephemeral_storage_mib": 512000,
    "contributed": true,
    "labels": {"zone": "campus-core"},
    "gpus": [{"gpu_id": "central-01-gpu-0", "model": "H100", "memory_mib": 81559, "profile": null}]
  }]
}
```

`state` ∈ `Ready|Draining|NotReady`；`profile` 为 MIG-like 切分或 null；`node_id` / `gpu_id`
必须全局唯一。文件在进程启动时读取，修改后需重启；`reset` 回到该文件的初始内容。

## 四个自由结构字段

- `Capacity.nodes[]`：`{node_id, state, gpus: [{gpu_id, model, memory_mib, profile, held_by}], cpu_millicores, memory_mib, ephemeral_storage_mib, contributed, labels}`
- `Capacity.totals`：`{nodes, gpu_total, gpu_free, gpu_allocated, gpu_contributor_held, gpu_unavailable, cpu_millicores, memory_mib, ephemeral_storage_mib}`
- `Allocation.placement`：`{node_id, gpu_ids[], bound_at}`
- `Allocation.preemption`：`{demand_id, requested_at, deadline, gpu_ids[], checkpoint_status, lost_seconds, expected_return_at, restarts_used, restarts_left}`

`held_by` = 持有它的 **allocation id** / `"contributor"` / `null`。
`totals.gpu_total = gpu_free + gpu_allocated + gpu_contributor_held + gpu_unavailable`（I3）。
`observed_at` 写逻辑时间 = 进程启动时刻 + 真实流逝 + `/_test/clock` 累计偏移，不是墙钟当前时间。

## 已实现的端点（21 个）

`/healthz`、`/v1/version`、`/v1/capacity`、`/v1/allocations`（POST/GET）、
`/v1/allocations/{id}`（GET/DELETE）、`.../renew`、`.../checkpoint`、`.../resume`、
`/v1/jobs`（POST/GET）、`/v1/jobs/{id}`（GET/DELETE）、`/v1/events`、`/v1/metrics`、
`/_test/reset`、`/_test/clock`、`/_test/contributor-demand`、`/_test/node-state`、`/_test/fault`。

真实行为：header 校验与错误体、跨 owner 一律 404、model 级候选匹配、单节点多卡原子绑定、
`Allocated ↔ Running` 的 Job 联动、终态 DELETE 幂等 202、`renew` 省略 ttl 沿用原值、
201 发 `allocation.created` + `allocation.allocated` 两条事件。

## 已知限制（后续步骤替换）

- 无显存 / profile / `node_selector` / CPU / 内存硬约束（第 3 步）。
- `/_test/clock` 只累加偏移，没有按时间排序的到期队列；TTL / queue_timeout 不自动到期（第 2 步）。
- 幂等键未按 `(owner, key)` 去重，也未做同 key 不同体的 409（第 3 步）。
- `PreemptRequested` 仍只由 `/_test/contributor-demand` 触发；三条归还分支、`Preempted` 时序、
  `resume` 的 200/202 路径未实现（第 6 步）。
- `/_test/node-state` 只改状态，`NotReady` 不驱逐其上 Allocation；`/_test/fault` 只记录配置（第 7 步）。
- Job 不会自行结束（第 5 步）。
- 放置固定为按节点顺序 first-fit，尚无 `PLACEMENT` / `THETA` 开关（第 9 步）。
- 原演示版 `/api/*` 产品扩展端点已移除；前端由 B 的第 4 步接入 `/v1/*`。

## 验证

```bash
python -m pytest -q
```

`tests/test_fabric.py` 覆盖：端点契约、错误体、租户隔离、资源文件加载与校验、`gpu_id` 稳定性、
capacity 账本守恒、`held_by` 语义、逻辑时间 `observed_at`、renew 省略 ttl、事件两条式写入。
