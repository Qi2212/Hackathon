# CampusGrove · 校园 GPU 算力互助平台

CampusGrove 将学校、实验室、协作组和个人的 GPU 放进同一个可发现、可申请、有授权与记录的互助流程。借用者可以查看资源与卡主计划，分析实验需求，比较方案并追踪任务；贡献者可以登记模拟工作站、发布资源并处理个人 GPU 的共享审批。

当前版本是**由后端接口驱动的本地系统流程 Demo**：使用合成资源、任务与历史样本，操作会改变本地模拟状态，不会在真实 GPU 上执行训练。底层服务及部分历史文件仍使用 Campus GPU Fabric / Aurora 名称。

## 文档入口

- [本地运行说明](docs/LOCAL_RUN.md)：Windows 安装、双服务启动、干净演示场景、账号、参数与排错。
- [两分钟演示讲稿](docs/DEMO_2MIN.md)：当前路演使用，含提前准备、操作顺序与逐段口播。
- [完整演示与答辩讲稿](docs/DEMO_SCRIPT.md)：8–10 分钟完整稿、3 分钟短稿、私有卡审批加演与答辩问答。
- 页面内的「使用指南」：项目介绍、借用与贡献流程、预测说明及五分钟演示脚本。

## 当前功能

| 模块 | 当前行为 |
| --- | --- |
| 测试身份 | 选择预制账号进入，无需密码；可以切换个人身份、任务、画像与审批视角 |
| 算力广场 | 按型号、来源和状态筛选；每台工作站的 3D GPU 每排四张，桌面端同时可见，窄屏横向滚动 |
| GPU 详情 | 单卡 / 整机详情、归属与规格、卡主项目计划曲线、24 / 48 / 72 小时条件共享窗口、当前任务和模拟收益 |
| 私有卡租用 | 列表选择工作站，直接填写卡数、15–60 分钟时长与可选留言；查看积分报价、发起审批、取消申请和批准后领取，无需任务分解 |
| 实验分析 | 一张表单共用研究目标和申请约束；本地模拟 API 返回任务拆解、候选节点、费用、条件时间与风险 |
| 资源方案 | 经济、算力、稳定、均衡四策略对比；修改输入后旧方案失效，确认提交复用已核对的方案 |
| 队列与任务 | 个人等待摘要、队列顺位、前序数量、阻塞原因和条件开始 / 结束时间；申请、运行状态展示与手动释放 |
| 个人画像 | 按当前个人展示项目、任务、个人资源及演示统计特征 |
| 资源与审批 | 实验室 / 协作组公共资源按身份过滤；个人私有资源可发现，借用需卡主授权；区分现场申请与预制审核案例 |
| 贡献资源 | 在线登记或 JSON 批量导入，核对草稿后显式发布；无活动租约时可撤回，记录持久保存 |
| 收支与事件 | 合成历史的来源、贡献者与平台收益拆分；本地交互、审批案例与事件回放 |

角色切换是演示机制，尚未接入生产身份认证。资源可见规则与真实集群的完整权限治理仍有区别。

### 直接租用私有卡

进入「发现算力 → 私有卡租用」（`/#rentals`），在列表点击「申请租用」，选择卡数和时长后提交。切换为列表标注的卡主，在「共享审批」同意；切回申请人，在「我的租用申请」点击「确认并领取」，再到「我的任务」查看并手动释放。此入口不调用任务分析和外部模型，资源固定到选择的工作站，卡槽由调度服务分配。

申请与审批均不预留容量。领取时容量不足会保留授权并提示稍后重试；时长从分配成功开始，单次最长 1 小时。当前仅为本地模拟，租用申请与扩展审批保存在扩展服务内存中，重启后清空；当前版本不支持预约未来的固定时段、自动领取或真实容器租用。

## 快速启动（Windows / PowerShell）

需要 Python 3.11+ 和浏览器。演示电脑无需 GPU / CUDA、Excel、数据库、Redis 或 Node.js 构建环境。首次安装依赖需要网络，默认演示无需外部模型服务。

### 首次安装

```powershell
Set-Location 'D:\spring_boot_content\Hackathon'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

仓库已附带演示种子。仅在 `app/data/demo_seed.json` 缺失或修改合成场景后重新生成：

```powershell
.\.venv\Scripts\python.exe -m tools.build_demo_seed
```

### 启动演示

```powershell
$env:FABRIC_DEMO = '1'
$env:LLM_PROVIDER = 'none'
$env:RELOAD = '0'
$env:FABRIC_EXT_AUTOSTART = '1'
$env:PORT = '8080'
$env:EXT_PORT = '8090'
$env:FABRIC_BASE_URL = 'http://127.0.0.1:8080'
$env:EXT_BASE_URL = 'http://127.0.0.1:8090'
.\.venv\Scripts\python.exe run.py
```

打开 **http://localhost:8080**，保持终端运行。不要直接双击 `index.html`。停止时在启动终端按 Ctrl + C；修改前端后可按 Ctrl + F5 刷新，修改后端后需要重启。

`run.py` 默认拉起两个服务：A 资源服务（8080）和 B 任务扩展服务（8090）。使用上述命令时，不要再单独运行 `run_ext.py`。

| 地址 | 用途 |
| --- | --- |
| http://localhost:8080 | 当前产品界面 |
| http://localhost:8080/healthz | 资源服务健康检查 |
| http://localhost:8090/ext/v1/healthz | 扩展服务及上游连通性检查 |
| http://localhost:8080/docs | A 侧接口文档 |
| http://localhost:8090/docs | B 侧接口文档 |
| http://localhost:8090/ui/ | 独立接口工作台，作为调试补充 |

如需更换端口、分别启动或排查连接，请看[本地运行说明](docs/LOCAL_RUN.md)。分别启动时，先设置 `FABRIC_EXT_AUTOSTART=0`，再在配置一致的两个终端分别运行 `run.py` 和 `run_ext.py`。

## 推荐体验路线

**林同学登录 → 查看 GPU 与卡主计划 → 提交实验并分析 → 生成方案与预估 → 核对后申请 → 我的任务 → 主动释放。**

主流程推荐使用公共资源，私有卡审批可作为加演：

| 输入 | 推荐值 |
| --- | --- |
| 实验类型 | 模型训练 |
| GPU | H100，1 张，单卡最低显存约 20 GB |
| 时长 / 预算 | 30 分钟 / 20 积分 |
| 分析范围 / 策略 | 公共大厅 / 综合均衡 |
| 截止时间 | 留空 |

新场景中公共 H100 参考价为 4.8 积分 / 卡时，上述使用估价约 2.4 积分。最终可申请状态以现场方案和提交校验为准。

修改预算为 0 会提示超预算；RTX 4090 配约 80 GB 显存会提示不兼容；任务类型会改变执行拆解，不允许中断会使分析推荐偏向稳定。分析不会自动改写用户输入，也不会占卡。

私有卡流程为：**申请人发起审批 → 切换卡主同意 → 切回申请人确认提交**。预制审核案例用于展示批复状态，不会自动创建现场任务。

## 演示账号与数据

| 主测试账号 | 推荐用途 |
| --- | --- |
| 林同学 | 借用与贡献、个人画像、主流程演示 |
| 周同学 | 私有卡贡献与审批，拥有 bio-ws-01 的 RTX 3090 |
| 尹同学 | 独立用户与大厅个人 GPU 贡献 |
| 蔡老师 | 实验室和协作组视角 |

另有其他预制成员，可从登录页选择；左下角账号入口支持切换身份。

默认种子包含 12 台工作站、34 张 GPU、20 个运行任务和 12 个等待任务；初始为 10 张空闲、2 张卡主自用、2 张离线，其余被任务占用。这是全局初始口径：不同身份可见范围不同，现场操作或已发布资源也会改变数字。

| 数据 / 文件 | 作用 |
| --- | --- |
| [app/synthetic.py](app/synthetic.py) | 合成资源、用户、任务、历史收益与利用率样本，不读取 Excel |
| [app/data/demo_seed.json](app/data/demo_seed.json) | 运行时加载的演示种子 |
| [app/data/experience_plans.json](app/data/experience_plans.json) | 个人项目计划样例 |
| `.app/demo_intelligence/` | 默认意愿分析、交互审计与审批案例记录 |
| `.app/demo_contributions/` | 默认用户登记、发布与撤回记录 |
| [resources.json](resources.json) | 非演示模式或演示种子不可用时的基础资源配置；不是默认的 34 卡场景 |

演示价目为积分 / 卡时：H100 4.8、A100 4.2、RTX 4090 3.2、RTX 3090 1.9。它们用于演示成本和激励，不是市场报价，也不代表真实支付或提现。

场景使用固定逻辑时间锚点，加上进程运行时间与测试时钟偏移。页面日期可能不是今天；租约到期时间也不等于真实实验完成时间。

### 重启与记录保留

- 核心资源任务场景重启后恢复种子；现场任务、租约、B 侧草稿、现场审批与账本主要保存在进程内存中。
- 意愿分析、交互审计、审批案例处理及用户登记资源写入本地文件；使用同一目录重启后保留，已发布资源会重新加载。
- 浏览器保存当前所选身份；可在页面切换或退出体验。

需要干净场景时，在启动前指定新的会话目录，无需删除原有数据：

```powershell
$env:CAMPUS_DEMO_DATA_DIR = Join-Path (Get-Location).Path ('.app\demo-session-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
```

然后运行 `run.py`。再次使用同一路径会保留该会话的文件记录，核心运行任务仍按上述规则重置。

## 配置与 API key

启动脚本会读取项目根目录 `.env`，已有进程环境变量优先。上面的启动命令显式使用本地规则模式，适合稳定演示。

| 变量 | 默认 / 用途 |
| --- | --- |
| `FABRIC_DEMO` | 启动脚本默认设为 1，加载演示场景 |
| `PORT` / `HOST` | A 服务端口 / 监听地址，默认 8080 / 0.0.0.0 |
| `EXT_PORT` / `EXT_HOST` | B 服务端口 / 监听地址，默认 8090 / 0.0.0.0 |
| `FABRIC_EXT_AUTOSTART` | 默认 1，随 run.py 启动 B；分别启动时设为 0 |
| `FABRIC_BASE_URL` | B 访问 A 的地址；启动器未发现已有配置时按 PORT 设置 |
| `EXT_BASE_URL` | A 转发扩展请求的地址；未设置时按 EXT_PORT 生成 |
| `CAMPUS_DEMO_DATA_DIR` | 演示文件存储位置；未设置时使用项目 `.app/` 下的默认目录 |
| `RELOAD` | A 开发热重载，默认关闭；PowerShell 中设为 1 后启动 |
| `TEST_KEY` | `/_test/*` 控制接口的密钥，默认 campus-test-key；与模型 key 无关 |
| `RESOURCES_FILE` | 基础资源文件路径，默认 resources.json；演示种子优先 |
| `LLM_PROVIDER` | 默认 none；可选 openai_compatible 或 qwen 等适配模式 |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 可选模型服务地址、模型名和密钥，填写到根目录 .env |
| `LLM_TIMEOUT_SECONDS` | 可选模型调用超时，默认 10 秒；不可用时回退规则 |
| `PROFILES_FILE` | B 侧估算器可选历史 Profile JSON；未设置时使用启发式估算 |

**当前产品提交页的模拟分析不需要真实 API key。** 模拟分析与可选模型接入是两条路径；配置真实模型不会把本次提交页的模拟分析自动变成真实调用。模型适配可用于 B 侧任务解析等可选路径，配置或环境变量修改后需重启。

密钥只放本机 `.env` 或环境变量，不写进 README、前端代码或示例文件。`.env` 已在 `.gitignore` 中排除。

## 项目结构与服务关系

```text
浏览器（8080，app/static/）
  ├─ /api/*           产品聚合、模拟分析、画像、资源登记
  ├─ /v1/*            A：容量、租约、任务、事件
  └─ /ext/* ───────── B（8090）：解析、估算、方案、决策、审批
                         └─ 通过 A 的 API 读取容量并提交资源申请
```

资源绑定与占用由 A 维护。B 根据快照生成方案，通过 A 的接口执行申请，并维护自己的任务草稿、决策、审批和结算记录。

| 目录 / 文件 | 内容 |
| --- | --- |
| [app/](app/) | A 资源服务与产品演示接口 |
| [app/static/index.html](app/static/index.html) | 当前页面入口，使用 workspace.css / workspace.js |
| [ext/](ext/) | B 任务扩展、四策略、审批和结算服务，附独立调试工作台 |
| [api/openapi.yaml](api/openapi.yaml) | 核心资源 API 契约，不包含所有产品演示接口 |
| [api/challenge.md](api/challenge.md) | 赛题与接口背景 |
| [tools/](tools/) | 合成种子生成器、测试客户端、场景回放与策略实验 |
| [tests/](tests/) | 核心、产品接口与扩展服务测试 |
| [docs/](docs/) | 当前本地运行说明和演示讲稿 |
| [reports/](reports/) | 历史审计、场景和实验报告；结论与数字需结合对应版本理解 |

### 主要接口

| 范围 | 示例 |
| --- | --- |
| 资源与任务 | `/v1/capacity`、`/v1/allocations`、`/v1/jobs`、`/v1/events`、`/v1/metrics` |
| 生命周期操作 | allocation 的 renew、checkpoint、resume，以及任务 / 租约的释放操作 |
| 产品展示 | `/api/dashboard`、`/api/demo/accounts`、`/api/demo/profile`、`/api/demo/nodes/{node_id}` |
| 模拟分析与预测 | `/api/demo/intelligence/intent`、`/api/demo/preview` |
| 资源登记发布 | `/api/demo/contributions`、`/import`，及具体记录的 publish / withdraw 操作 |
| 扩展任务流程 | `/ext/v1/tasks/analyze`，以及任务的 confirm、estimate、plan、decisions |
| 扩展审批与结算 | `/ext/v1/approvals`、`/ext/v1/decisions/{decision_id}/submit`、settle 及 billing 接口 |
| 演示控制 | `/_test/reset`、`/_test/clock`、`/_test/contributor-demand`、`/_test/node-state`、`/_test/fault` |

核心用户操作使用 `X-Owner` 标识归属，测试控制使用 `X-Test-Key`。创建 allocation 的 `Idempotency-Key` 按 owner 隔离，同体重放返回首次结果，复用同一 key 提交不同内容会冲突。演示身份头不能替代生产鉴权。

部分产品接口使用 `include_in_schema=False`，不会出现在 `/docs`，可在 `app/presentation.py`、`app/experience.py`、`app/intelligence.py` 和 `app/contributions.py` 查看定义。

## 验证与开发工具

单元与接口测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖核心资源生命周期、幂等与归属、节点异常与抢占测试控制、任务解析与估算、四策略、审批与结算、产品视图、模拟分析、资源登记发布等。通过数量以本次执行结果为准。

工具还提供场景回放与配对实验：

```powershell
.\.venv\Scripts\python.exe -m tools.run_scenarios --list
.\.venv\Scripts\python.exe -m tools.run_scenarios --verbose
.\.venv\Scripts\python.exe -m tools.run_experiment
```

场景回放和实验需要已启动的服务，会复位资源并产生测试任务；请在专用测试实例上执行。历史报告中的通过数、价格和策略差异不是当前场景的固定结果。

## 当前边界与后续工作

- **真实计算接入：**未接入真实 GPU 节点 Agent、硬件探测或训练执行；3D 卡片与任务状态对应模拟资源。
- **预测与曲线：**卡主曲线来自预制计划，画像与概率是演示统计基线；开始 / 结束时间属于条件估计，未完整计入续租、新竞争和审批耗时，不构成预约。
- **自动调度：**尚未实现完整 Pending 自动派发、TTL / queue timeout 自动到期，以及任务自然结束；演示中主动申请并手动释放。
- **共享治理：**已实现演示身份下的资源可见规则和私有卡审批流程；自助小组创建、邀请、成员管理及完整分层匹配仍需完善。
- **生命周期：**已有规格约束、幂等、节点 NotReady 驱逐和测试时钟推进抢占通知到期等行为；完整自动回收、迁移和恢复闭环仍需继续建设。
- **收益：**页面近 30 日收支来自合成历史，不随现场任务自动更新；B 侧另有结算接口，尚未形成真实支付或提现体系。
- **部署：**默认以两个本地进程和文件 / 内存保存状态，无生产认证、数据库持久化与高可用部署。

项目当前重点是验证校园算力互助的产品流程、资源归属、需求约束、方案解释和授权协作。完整演示方式见[现场讲稿](docs/DEMO_SCRIPT.md)。
