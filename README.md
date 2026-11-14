# Aurora Campus GPU Fabric

一个可运行的校园 GPU 算力互助平台原型：FastAPI 事件驱动模拟后端 + 原生 HTML/CSS/JavaScript 多角色控制台。

核心目标不是“把卡分出去”，而是在租约到期、贡献者收回、重复请求和节点故障同时出现时，仍然保证资源账本正确，并且能解释任务为什么停下、资源何时归还。

## 快速启动

```bash
cd /Users/qi/Desktop/h
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

浏览器打开 <http://localhost:8080>。API 文档位于 <http://localhost:8080/docs>。

测试控制接口默认密钥：`campus-test-key`。生产部署应通过环境变量 `TEST_KEY` 替换，并隔离 `/_test/*` 网络访问。

## 已实现

- 冻结公共 API：健康检查、版本、容量、Allocation、租约续期/释放、Checkpoint/Resume、Job、事件与指标。
- 测试控制 API：重置、逻辑时钟、贡献者收回、节点状态、429/5xx/延迟故障注入。
- 原子内存账本：单进程 `asyncio.Lock` 保护检查与扣减；终态统一归还 GPU。
- `Idempotency-Key`：同一 Owner + Key 只创建一次分配。
- 租约：逻辑时钟推进后自动 `Expired` 并归还资源。
- 贡献者抢占：`Running → PreemptRequested → Preempted → Pending/Allocated`；通知期结束一定归还，不被 Checkpoint 成败阻塞。
- 节点故障：`NotReady` 后相关分配进入 `Evicted`，同步清理账本。
- 私有卡审批：AI 匹配命中私有卡时先 `Pending`，实验室批准后才实际扣减资源。
- 实验室画像：根据硬件适配、可靠性、回收风险、DDL 距离、历史实验室关系输出可解释评分。
- 多角色前端：全局资源、普通/实验室任务提交、任务租约、私有卡审批、服务器挂载、贡献收益、画像和事件时间线。

## 状态与关键不变量

1. 任意 GPU 同一时刻最多属于一个活跃 Allocation。
2. `Released / Expired / Evicted / Rejected` 不占资源。
3. 私有 GPU 在审批通过前不进入账本占用。
4. 贡献者通知期结束时必须归还资源；Checkpoint 失败只影响恢复质量，不影响归还权。
5. 相同 `(Owner, Idempotency-Key)` 返回同一个 Allocation。
6. Resume 不突破 `max_restarts`，无兼容容量时进入 `Pending`，不会虚报恢复成功。

## AI 分配策略（模拟）

当前策略是确定性、可解释的加权评分，不声称是经过真实 GPU 性能验证的模型：

- 近 30 日节点可靠性：34% 基础权重；
- 显存贴合度：避免用大卡承载明显过小任务；
- 回收风险：低/中/高分别施加 0/7/15 的惩罚；
- 公共资源优先：减少审批延迟；
- DDL 紧迫度：越临近截止日期，越偏向低回收风险节点；
- 实验室关系：同实验室历史协作提供小幅加权。

前端会展示每个候选节点的得分和原因。真实生产版本应接入可校准的任务 Trace、审批 SLA 与中断统计，并与 FIFO/最小碎片等基线做配对实验。

## 验证

```bash
source .venv/bin/activate
pytest -q
```

测试覆盖幂等去重、租约自动归还、贡献者收回与恢复、节点故障账本清理、私有卡审批前不占卡。

## 已知限制

- 当前使用进程内内存状态，重启即重置；多副本部署需迁移到事务数据库或带 CAS 的一致性存储。
- Job 是控制面模拟，不真实执行 CUDA 容器。
- 画像数据和贡献积分是演示样本，未接入真实课题组身份与结算系统。
- 挂载流程模拟请求与校验步骤，未包含真实 Agent 安装包。
- 公共接口按提供的 OpenAPI 行为实现；产品扩展统一放在 `/api/*`，避免改变冻结契约。

## 目录

```text
app/main.py          FastAPI、状态机、账本与扩展 API
app/static/          多角色单页控制台
tests/               关键可靠性测试
requirements.txt     锁定依赖
run.py               本地启动入口
```
