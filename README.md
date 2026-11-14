# Campus GPU Fabric · Server

> 当前实现核对见 [reports/implementation_audit.md](reports/implementation_audit.md)。Aurora 已接入接口；run.py 默认同时启动 8080 资源服务和 8090 扩展服务，打开 http://localhost:8080。首次运行先安装 requirements.txt，再执行 python -m tools.build_demo_seed。API key 配置见 .env.example。下文旧有“P0–P9 完成”仅指 B 侧阶段，不表示总体设计文档全部完成；TTL 自动释放、Pending 自动派发、完整小组权限与分层匹配仍未实现。



校园 GPU 算力互助平台的**服务端**，按冻结的 `api/openapi.yaml`（v2.0.0）实现。
A（`app/`，8080）提供容量、allocation、任务、事件与测试时钟；B（`ext/`，8090）提供
任务解析、估算、策略、决策、审批、结算、可用性预测与工作台。默认以
[演示快照](#演示模式excel-快照数据)（29 节点 / 84 卡）启动，两端数字同源。

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
| `FABRIC_DEMO` | 空 | `1` 时启用演示快照（见下）；`run.py` / `run_ext.py` 默认置为 `1` |

### 演示模式（Excel 快照数据）

启动即回放 `app/data/Aurora_GPU_Fabric_演示数据.xlsx` 的静态快照，UI 与接口数字一致：

- **29 个节点 / 84 张 GPU**：73 张已分配、2 张卡主自用（`held_by="contributor"`）、
  8 张空闲、1 张因节点离线不可用；Pending 任务 40 条、待审批 4 条、利用率 89.3%。
- 93 条非终态 allocation 随进程启动注入，按 `X-Owner` 可查到各自的 Running / Pending 记录。
- 逻辑时间基线下锚到快照时刻 `2026-11-14T03:00:00Z`，`observed_at` 从此起算真实经过时间。
- 计费改为**积分**：单价取 Excel `hourly_credit` 的型号中位数
  （H100 4.8、A100 4.2、RTX 4090 3.2、RTX 3090 1.9 积分/GPU·小时），
  UI 由 `/ext/v1/overview` 的 `unit` 字段切换显示为“积分”。
- `GET /ext/v1/overview` 输出 6 个 KPI（GPU 总数 / 使用中 / 空闲 / 等待任务 / 利用率 / 待审批），
  工作台顶部据此渲染卡片。

重新生成种子数据（改了 Excel 后执行）：

```bash
python -m tools.build_demo_seed
```

产物 `app/data/demo_seed.json`（`nodes`、`allocations`、`pricing`、`kpis`、`labs`、`queues`），
构建期自检 GPU 守恒与孤儿持有者，`tests/test_demo_seed.py` 复验这些不变量。

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

## B 侧扩展服务（`ext/`，P0–P9 已完成）

独立进程，只读 A 的冻结 API，不导入 `app` 包、不维护第二份可写账本。

```bash
python run.py        # A：8080
python run_ext.py    # B：8090（另一终端）
```

打开 `http://localhost:8090/ui/` 即产品工作台（纯静态，无需构建）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FABRIC_BASE_URL` | `http://localhost:8080` | A 的地址 |
| `FABRIC_TIMEOUT_SECONDS` | `5` | 调 A 的超时 |
| `EXT_PORT` / `EXT_HOST` | `8090` / `0.0.0.0` | 扩展服务监听 |
| `LLM_PROVIDER` | `none` | `none` 走确定性解析；`openai_compatible` 启用 LLM |
| `LLM_BASE_URL` / `LLM_MODEL` | 空 | OpenAI 兼容端点与模型名（两者都填才生效） |
| `LLM_API_KEY` | 空 | 可选 Bearer token |
| `LLM_TIMEOUT_SECONDS` | `10` | LLM 调用超时，失败自动回退 |
| `PROFILES_FILE` | 空 | 历史 Profile JSON；为空则纯启发式估算 |

端点（全部为真实值，不是假数据）：

- `GET /ext/v1/healthz`：自身状态 + 上游 A 的 `reachable/status/logical_time/latency_ms`；
- `GET /ext/v1/version`：扩展版本、契约版本、上游 `api_version`；
- `GET /ext/v1/overview`：工作台顶部 KPI（含 `demo` / `unit` / `source` / `observed_at`）；
- `GET /ext/v1/snapshot`：读取 `/v1/capacity` 并归一化为 `ResourceSnapshot`
  （`available_gpus`、`model_index`、每个节点的 `schedulable/free_gpu_ids`）；
- `GET /ext/v1/contracts`：`TaskSpec`、`WorkloadEstimate`、`ResourceSnapshot`、`StrategyPlan`、
  `PlacementDecision`、`Approval` 等契约的 JSON Schema；
- `POST /ext/v1/tasks/analyze`：自然语言 → `TaskSpec`，同时返回 `context`
  （`known_models`、`snapshot_observed_at`、`snapshot_error`、`llm`）；
- `GET /ext/v1/tasks`、`GET /ext/v1/tasks/{task_id}`：任务草稿列表 / 详情；
- `POST /ext/v1/tasks/{task_id}/confirm`：补全或修正字段，`confirm=true` 时才置为 `confirmed`；
- `POST /ext/v1/tasks/{task_id}/estimate`：估算资源规格，返回 `estimate` 与
  可直接提交给确认接口的 `suggested_answers`；
- `GET /ext/v1/tasks/{task_id}/estimate`：读取该任务最近一次估算结果；
- `POST /ext/v1/tasks/{task_id}/plan`：基于当前 `/v1/capacity` 生成四策略方案
  （Economy / Fast / Stable / Balanced），返回 `plan` 与所用 `estimate`；
- `GET /ext/v1/tasks/{task_id}/plan`：读取该任务最近一次方案；
- `POST /ext/v1/tasks/{task_id}/decisions`：把选定策略落成 `PlacementDecision`（需 `X-Owner`，201）；
- `GET /ext/v1/tasks/{task_id}/decisions`、`GET /ext/v1/decisions/{decision_id}`：决策与提交结果；
- `POST /ext/v1/decisions/{decision_id}/submit`：提交给 A 的 `POST /v1/allocations`，
  透传 201/202，冲突映射 409；
- `POST /ext/v1/decisions/{decision_id}/approvals`：发起 Owner 审批（含可选预约窗口），201；
- `GET /ext/v1/approvals`（按 `owner_id/requester/status/decision_id` 过滤）、
  `GET /ext/v1/approvals/{approval_id}`：审批列表与详情；
- `POST /ext/v1/approvals/{approval_id}/decision`：Owner 接受 / 拒绝 / 还价；
- `POST /ext/v1/approvals/{approval_id}/cancel`：申请人取消待审批；
- `GET /ext/v1/accounts`、`GET /ext/v1/accounts/{owner}`：信用账户；
- `POST /ext/v1/contributions`（`X-Owner` + 可选 `Idempotency-Key`）、
  `GET /ext/v1/contributions`：贡献登记；
- `POST /ext/v1/decisions/{decision_id}/settle`：按 A 的实际生命周期结算；
- `GET /ext/v1/billing/records`、`GET /ext/v1/billing/records/{record_id}`：消费明细；
- `POST /ext/v1/availability/observe`、`GET /ext/v1/availability/forecast`、
  `GET /ext/v1/availability/observations`、`POST /ext/v1/availability/reset`：可用性观测与预测。

`observed_at` 透传 A 的逻辑时间；`fetched_at` 是 B 的墙钟时间，两者不可混用。
上游失败统一返回 502，错误体与 A 一致：`{error, detail, request_id}`。
数据契约在 [ext/schemas.py](ext/schemas.py)，版本 `1.5.0`；测试 [tests/test_ext.py](tests/test_ext.py)
用 `httpx.MockTransport` 模拟 A，不依赖 8080 进程。

### Task Analyzer（P1）

解析顺序固定为：**确定性规则 > LLM 补全 > 策略默认值**。

- 规则解析：任务类型、GPU 型号/张数、显存、CPU/内存/存储、时长、截止时间（`本周三` / `下周三` /
  `11月20日` / `3天内`）、预算、可中断/断点/紧急程度；命中项在 `provenance` 里标为
  `user_explicit`。
- LLM 只补缺失字段，置信度上限 0.75 并标 `inferred`；`gpu_models` 必须出现在
  `/v1/capacity` 的候选型号里，否则丢弃；过去的 `deadline` 丢弃；LLM 超时/报错自动回退。
- 缺失关键信息生成 `missing` 追问：任务类型未知、训练类任务没有 GPU 张数为 `blocking`；
  时长/型号为 `recommended`；预算/截止为 `optional`。
- `status` 取值：`draft`（任务类型未知）、`needs_confirmation`（含推断或待补全）、
  `confirmed`（仅确认接口可写入）。
- 确认接口支持 `gpu_count`、`gpu_models`、`duration_seconds`、`deadline`、`max_cost`、
  `interruptible` 等字段；仍有 `blocking` 缺项时返回 400 `blocking_fields_missing`，
  非法值返回 400 `invalid_answer`。

### Workload Estimator（P2）

- 输入 `TaskSpec`，输出 `WorkloadEstimate`；**不回写 TaskSpec**，估算结果单独保存。
- 显存经验值（GiB / 每 B 参数）：全量训练 16、全量微调 16、LoRA 3、QLoRA 1.5、推理 2；
  未识别参数量时按任务类型默认值（训练 40、微调 24、推理 16、数据处理 8）。
- GPU 张数 = ceil(显存需求 / 参考单卡显存)，参考：训练类 80、推理/Notebook 24、仿真 48，
  上限 16；推荐的 GPU 型号只表示需求档位，不代表集群当前有卡。
- CPU / 内存 / 存储 / 时长按任务类型与数据规模推导；用户已给出的字段一律保留。
- `PROFILES_FILE` 提供历史 Profile 时，按 `task_type` 与参数量级（0.5x~2x）匹配，
  时长与张数取中位数后与启发式平均，`basis=history`、`sample_size=匹配条数`。
- `confidence` 由信息完整度累加（任务类型 / 参数量 / 数据量 / 时长 / 张数 / 历史），
  任务类型未知时上限 0.4。
- `suggested_answers` 可直接提交给确认接口，串起“估算 → 确认 → 已确认”的闭环。

### Matcher 与四策略（P3）

- 硬约束（不满足即不可放置）：节点 `Ready`；同一节点上空闲 GPU 数量够；
  型号在需求集合内；单卡显存 >= 下限；节点标签包含全部偏好；节点 CPU/内存/存储容量足够。
  A 的 capacity 不暴露 CPU/内存已用量，因此这里是容量级校验。
- GPU 选择：Economy 选价目最低且能装下的卡；其余策略选算力最高的卡。
- 指标：`expected_cost`（静态价目 × 张数 × 时长）、`stability`、`interruption_risk`、
  `fit_score`（显存贴合度）；全部来自当前快照，不预测未来。
- 策略取向：
  - `economy`：成本最低；
  - `fast`：算力分最高、可立即绑定；
  - `stable`：稳定性最高（空闲率高、非贡献节点优先）；
  - `balanced`：成本 / 稳定性 / 匹配度 / 算力的加权和，默认推荐。
- 没有可行节点时四个选项都 `feasible=false`、`placement=null`，`plan.notes` 给出阻塞原因；
  绝不在不可行时假装分配成功。
- 演示价目（CNY/GPU·小时）：H100 12、MI300 8、A100 6、L40S 5、A10 3、RTX 4090 2.5、
  V100 2、RTX 3090 1.8，未知型号 4。P6 权益模块会复用并校准。

### PlacementDecision 与提交（P4）

- 决策归属：创建决策时必须带 `X-Owner`，并写入 `allocation.owner`；提交时用存储的 owner，
  若请求头与决策 owner 不一致返回 403 `owner_mismatch`。
- 请求体映射：GPU 张数取方案建议；型号在所选 GPU 型号一致时传具体型号，否则传 `any`；
  显存下限、CPU/内存/存储取 TaskSpec 优先、估算补空；标签取 `preferred_labels`。
- `preemption_policy.mode` 由中断要求推导：不可中断 → `non_preemptible`；
  可中断且支持 checkpoint → `checkpoint`；否则 `restartable`。
- `gpu_ids` / `node_id` 只是建议值，冻结 API 无法按 gpu_id 绑定，权威结果以 A 返回的
  `placement` 为准；`ttl_seconds` 上限 3600，更长任务需 renew。
- 幂等：默认用 `decision_id` 作为 `Idempotency-Key`，可在提交体覆盖；同一决策重试不会换 key。
- 状态透传：A 201 → 201 已绑定；A 202 → 202 排队；A 409 → 409 `allocation_conflict`；
  A 429 → 429 `rate_limited`；其余上游错误 → 502。提交结果（含失败）都会保存在决策上。
- `requires_approval=true` 的决策在 `approval_id` 为空时提交返回 409 `approval_required`，
  审批流程见下节 P5。

### Owner 审批与预约（P5）

- 审批是产品层授权记录，**不占用 A 的 GPU**；冻结 API 没有预留容量接口，预约窗口
  只约束“何时可以提交”，最终绑定仍由 A 原子完成。
- 流程：申请人（决策 owner）创建审批 → Owner（`X-Owner` 必须等于 `owner_id`）
  接受 / 拒绝 / 还价 → 接受后 `approval_id` 写回决策 → 提交时校验。
- 一个决策同时最多一个 `pending` 审批；重复创建返回 409 `approval_exists`。
- 窗口用 UTC ISO-8601，必须成对出现、`end > start` 且未过期；否则 400 `invalid_approval`。
- 提交校验：无审批 → 409 `approval_required`；非 accepted → 409 `approval_not_accepted`；
  accepted 但当前时间不在窗口内 → 409 `outside_reservation_window`。
- 拒绝/还价不会写回 `approval_id`，因此仍无法提交；申请人可取消 `pending`。
- 注意：审批状态里的 `expired` 目前不会被自动推进（B 没有后台定时器），需要时由调用方
  显式处理或重新发起。

### 贡献、使用与信用结算（P6）

- 贡献：`credit = 单价 × 张数 × 小时 × 1.2`；`Idempotency-Key` 作用域 `(owner, key)`，
  重复提交返回首次记录（200），不会重复入账。
- 使用：`cost = 单价 × 实际张数 × 实际小时`，张数取 A 的 `placement.gpu_ids`，
  型号按 `/v1/capacity` 的 `gpu_id → model` 反查。
- 结算时间：优先取 A 的终态事件（`allocation.released/expired/evicted`）；
  A 尚未产生事件时退回 `capacity.observed_at`。**绝不用 B 的墙钟去减 A 的 `bound_at`**，
  否则两套时间轴会把时长算成 0。
- 只结算终态 allocation（Released / Expired / Evicted）；未终态返回 409
  `allocation_not_terminal`，未成功提交返回 409 `allocation_not_submitted`。
- 结算按决策幂等：同一决策只有一条 `BillingRecord`，重复调用返回 200 与首次结果。
- 账户：`balance = contributed - consumed`，允许为负；同时累计贡献/消费的 GPU 小时。
- 局限：贡献目前是**自报登记**（B 无法从冻结 API 得知节点归属），报告里必须注明。

### 可用性预测（P7）

- 只使用 B 能看到的信息：`/v1/capacity` 快照序列中的 `state`、`contributed`、GPU `held_by`，
  不读取 A 的未来调度，也不猜测 Owner 行为。
- 观测按 A 的逻辑时间 `observed_at` 去重：同一逻辑时刻重复拉取只覆盖最后一条，不会刷高样本量。
- `availability_score`：空闲率、是否 Ready、趋势、是否贡献节点的加权；NotReady 上限 0.15。
- `interruption_risk`：基础 0.1 + 占用率 + 贡献节点 0.2 + NotReady 0.25（且下限 0.65）+ 下降趋势 0.1。
- `expected_free_gpus`：当前空闲 + 近 4 次趋势外推，按 horizon 截断；NotReady 直接为 0。
- `confidence`：样本数 1 → 0.4，随样本增加最高 0.9；`reasons` 写明依据。
- 接入策略：`build_plan` 收到预测时按 `0.5 × 预测风险 + 0.5 × 静态规则` 修正
  `interruption_risk`，样本不足也不会让预测完全接管。
- `POST /tasks/{id}/plan` 的响应新增 `forecasts`，与方案同源同时刻。

### 前端工作台（P8）

- 地址：`http://localhost:8090/ui/`，由 `ext/static/` 直接托管，无构建步骤。
- 流程：描述任务 → 分析/估算 → 四策略方案对比（含预测） → 创建决策 →
  提交给 A →（可选）Owner 审批 → 贡献/结算。
- 侧栏：集群容量（GPU 空闲/已分配/贡献者）、待审批列表、信用账户与消费明细。
- Owner 身份从右上角输入，存 `localStorage`；审批按钮以该审批的 Owner 身份执行，
  这是单机演示的简化，不是真实鉴权。
- 所有写操作都走已实现的 `ext` 接口，前端不直接访问 A，也不存第二份账本。
- 验证：`tests/test_ext_ui.py` 检查页面与静态资源；浏览器实测“分析 → 生成方案”
  可看到四张策略卡片与 3 个节点。

### 测试客户端与场景回放（`tools/`）

```bash
python -m tools.run_scenarios --list
python -m tools.run_scenarios --verbose
python -m tools.run_scenarios --scenario basic_lifecycle --scenario owner_isolation
python -m tools.run_scenarios --json-out reports/scenarios.json
```

- [tools/fabric_client.py](tools/fabric_client.py)：A/B 两端测试客户端，4xx 不抛异常，
  便于断言错误语义；A 写操作带 `X-Owner`，`/_test/*` 带 `X-Test-Key`。
- [tools/invariants.py](tools/invariants.py)：容量守恒、GPU 归属、生命周期、所有权四类不变量。
- [tools/scenarios.py](tools/scenarios.py)：8 个场景（生命周期、幂等、隔离、排队不超卖、
  混合负载、NotReady 驱逐、抢占契约、扩展财务闭环），每个场景自带复位、清理与不变量断言。
- 运行器会等待 A 与扩展服务就绪；A 未实现的端点返回 404 时记为 `skipped`，有失败时退出码 1。
- 当前真机结果：`8 passed, 0 failed, 0 skipped`。原先的三项失败已补齐：
  幂等键 `(owner, key)` 同体重放/异体 409（A 第 3 步）、NotReady 驱逐占用态（A 第 7 步）、
  抢占到通知截止时归还 GPU 并写 `checkpoint.timeout` / `allocation.preempted` /
  `gpu.returned_to_pool`（A 第 6 步）。三项均有 `tests/test_fabric.py` 回归用例。
  结果保存在 `reports/scenarios.json`。

### 四策略配对实验（P9）

```bash
python -m tools.run_experiment
python -m tools.run_experiment --seed 7 --tasks 6 --strategies economy,fast
```

- 同一 seed 生成同一顺序的负载；每个策略前 `/_test/reset`，任务之间不释放 GPU 制造争用，
  结束后统一释放并跑不变量检查。
- 指标为**计划口径**：估算成本、平均风险、平均稳定性、绑定/排队/冲突/不可行、瞬时利用率。
- 产出 `reports/experiment.md` 与 `reports/experiment.json`；相对最低成本策略给出配对差异。
- 首轮（seed 2026，6 个任务）：economy / stable / balanced 均为 ¥20.50、风险 0.335；
  fast 为 ¥22.00、风险 0.369；四策略均 4 绑定、2 不可行、0 不变量问题。
- 发现：Workload Estimator 会按显存推荐**单一型号**（≤24GB 只给 RTX 4090），
  导致三个策略被锁在同一型号上、无法体现节点层面的差异；后续可改成只给显存下限、
  不锁型号，再复跑配对实验。
- A 的 `PLACEMENT=first_fit|risk_aware` 需要重启进程切换，本实验只比较 B 的四个策略。
