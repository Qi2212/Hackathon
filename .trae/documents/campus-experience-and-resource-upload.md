# CampusGrove 交互体验与资源贡献改造计划

## Context

当前“提交实验”同时展示 Agent 意愿和实际申请两套表单，首屏层级不清；GPU 状态仅以小块颜色表示；“队列与预测”以横向卡片堆叠，难以回答排第几、为什么等、何时有卡；“贡献资源”只有介绍，没有真实登记能力。

本次保持现有蓝色视觉、FastAPI A/B 边界和冻结的 `api/openapi.yaml` 不变，完成四个页面的统一体验改造，并真正实现“在线表单 + JSON 文件”资源登记。上传内容仅代表用户声明的 Demo 模拟资源，不冒充真实硬件探测或节点 Agent 接入。

## 当前状态分析（2026-11-15 代码复核）

- 资源贡献后端已经落地：`app/contributions.py` 提供登记、multipart JSON 导入、发布、撤回、用户隔离和原子持久化；`app/main.py` 的 `SkeletonState.reset()` 已按“内置 seed + 已发布节点”去重恢复；`app/experience.py` 已避免上传节点生成预制项目和历史收益。后台实现任务报告相关测试 25 项、全量测试 200 项通过，最终仍由主执行任务独立复跑确认。
- `app/presentation.py` 的 `schedule()` 已返回 GPU 需求、前序数量、兼容容量、阻塞原因、条件开始区间和预测依据；没有可靠候选时保持时间为 `null`。
- Qwen/OpenAI 兼容接入和规则回退已经完成；模型密钥仍只从环境变量读取，不在代码、日志或回复中输出。
- `app/static/index.html` 已出现新版三步提交流程和队列摘要容器，但本次读取时 `workspace.js` 的 `renderResources()`、`renderQueues()`、`renderProfile()`、`buildPlan()`、`requestTask()` 仍是旧实现，`workspace.css` 也仍是多层主题覆盖；另一个前端实现任务正在处理这些文件，必须等待其完成并审查实际差异，不能以 HTML 容器存在判定功能完成。
- `mount` 页仍是静态介绍，`workspace.js` 尚未请求 `/api/demo/contributions`，这是后端完成后必须补齐的主要缺口。

## 1. 提交实验：一页分步，按实验事项动态展示

修改 `app/static/index.html`、`app/static/workspace.js`、`app/static/workspace.css`。

- 保留 `intentForm` 和 `taskForm` 各自的后端职责，不强行合并数据模型；前端改为同一页面内的连续步骤：**描述意愿 → 确认资源与方案 → 最终提交**。
- Agent 结论优先展示推荐配置、任务拆解摘要和主要风险；详细运行轨迹放入可展开区域，预测图不再抢占首屏。
- Agent 分析完成后提供“采用建议配置”的明确操作，映射 GPU、数量、时长等字段；已手动修改的内容不被静默覆盖。
- 按实际实验类型展示相关设置：训练/微调突出检查点与截止期，批量推理/数据处理突出批量完成时间，交互实验突出使用时长；不相关设置收进“高级选项”。
- 用统一草稿保存意愿、实际约束、已选策略和预估结果。任何关键字段变化都将旧预估标记为过期；最终确认前显示完整摘要。
- 顺带修复当前“先看看预估”重复调用 Preview，以及提交时重新生成另一份 Plan 的问题，确保预览和提交使用同一份有效方案。

## 2. GPU 与个人画像：轻量 3D 状态视觉

修改 `renderResources()`、`renderProfile()` 和最终蓝色主题样式。

- 每张 GPU 使用纯 CSS/SVG 的等距小电脑，保留现有 `free / busy / self / offline` 状态优先级。
- 空闲显示可用勾号，使用中显示工作条，卡主自用显示人物/锁标识，离线显示灰色斜线并停止动画；同时提供状态文字和 `aria-label`，不只依赖颜色。
- 个人画像头部加入与身份、实验室信息组合的 CSS/SVG 3D 小人，不替代现有行为雷达和画像依据。
- 不引入 Three.js 或持续 JavaScript 动画循环；仅使用 transform/opacity，适配窄屏，并在 `prefers-reduced-motion` 下关闭动画。

## 3. 队列与预测：先回答用户最关心的问题

增强 `app/presentation.py` 中现有 `schedule()`，重做 `renderQueues()`，预测仍使用同一快照推演，不建立第二套算法。

- 队列条目补充 GPU 需求、有效前序任务数、兼容节点/卡数、主要阻塞原因、预计开始区间和预测依据；没有兼容资源或可靠租约时间时返回空时间及明确原因，不编造顺位或日期。
- 页面顶部改为“我的等待摘要”：当前顺序、前面还有几个任务、需要几张卡、预计等待区间和首要阻塞原因。
- 下方使用纵向队列表格/时间线，依次展示顺序、任务、资源需求、等待原因、预计开始和结束；自己的任务高亮，“只看我”仍保留前序等待解释。
- 可展开查看容量释放时间线，明确区分“租约已知”“条件推演”“暂时无法估计”；保留“未预约、未占卡、不会自动派发 Pending”的说明。

## 4. 贡献资源：真实登记、JSON 导入和发布管理

新增 `app/contributions.py`，并在 `app/main.py`、`app/presentation.py` 中受控安装；不修改 B 服务和冻结 OpenAPI。

### 接口与持久化

- `GET /api/demo/contributions`：当前用户的登记、状态和事件。
- `POST /api/demo/contributions`：在线登记一个工作站。
- `POST /api/demo/contributions/import`：multipart 上传一个 JSON 文件，批量校验和导入。
- `POST /api/demo/contributions/{id}/publish`：显式发布到 Demo 资源大厅。
- `POST /api/demo/contributions/{id}/withdraw`：撤回；存在活动 Allocation 时返回 409。
- 所有数据按 `X-Owner` 隔离，跨用户详情和操作返回 404。
- 使用独立的 `state.json + events.jsonl`、进程内锁和原子替换；默认保存到 `.app/demo_contributions`，测试时随 `CAMPUS_DEMO_DATA_DIR` 隔离。

### 校验边界

- JSON 文件最大 1 MiB，仅接受 `.json`；每批 1–10 个节点，每节点 1–8 张 GPU，整批最多 32 张。
- 严格校验字段、整数范围、GPU 型号和显存；拒绝未知归属字段、客户端自定义 ID、`held_by`、任意 Labels、凭证或执行命令。
- 节点/GPU ID 和 owner 归属由服务端生成；批量导入任一项失败则本批不创建资源，并返回具体字段错误。
- 文件内容直接解析，不保存原始上传文件，不将文件名拼入磁盘路径；`requirements.txt` 增加 multipart 支持。

### 与 Demo 账本衔接

- 状态采用 `registered → published → withdrawn`。登记成功不会立刻改变全校容量，用户点击发布后才进入资源大厅。
- 发布默认为 `personal_private`，仍要求卡主审批；仅表示“Demo 调度已启用”。
- A 的 `SkeletonState.nodes` 继续作为唯一调度账本。发布/撤回通过受控方法更新；重启和 `/_test/reset` 后按“内置 seed + 已发布资源”重建，避免丢失、重复和草稿混入。
- 非 Demo 模式及原有资源契约保持不变；上传资源详情不生成虚假的预制项目或历史收益。

### 页面

- 将 `mount` 页改为“在线登记 / JSON 导入”两个操作入口，提供模板、限制说明、实际上传状态和逐项错误。
- 下方展示“我的登记”：规格、状态、发布时间、调度节点、事件及发布/撤回按钮。
- 操作成功后同步刷新登记列表、算力广场和个人画像；更新使用指南中“尚未实现资源注册”的旧说明。

## 5. 全系统视觉升级与 Qwen 接入

- 在现有蓝色主题上建立统一设计令牌：字体层级、流体字号、间距、圆角、边框、阴影、状态色和动效时长；覆盖登录、侧边栏、顶部导航、指标卡、表格、表单、弹窗、空状态和移动端。
- 使用可靠的系统中文字体栈与清晰的标题/正文/辅助文字层级，不依赖外部字体网络；强化数据数字、英文 eyebrow、按钮和表格的可读性。
- 科技感主要来自蓝色层次、细网格/光晕、细腻渐变和轻量状态动效，避免大面积高饱和发光、无意义动画及信息密度过高。
- 基于 `ext/llm.py` 现有 OpenAI 兼容链路支持用户指定的 Qwen3.8-27B，模型名、Base URL 和 API Key 均通过环境变量配置；兼容 `DASHSCOPE_API_KEY` / `QWEN_API_KEY`，不读取后打印、不写入仓库。
- Agent 页面明确显示实际 provider/model 和规则回退状态；大模型仍只辅助语义理解，不覆盖结构化硬约束。密钥未就绪时保持完整规则模式，不影响资源与提交流程。

## 6. 验证

- 新增 `tests/test_contributions.py`：在线登记、合法/非法 JSON、文件上限、批量原子性、用户隔离、发布/撤回、活动 Allocation 阻止撤回、重启和 reset 恢复、非 Demo 不受影响。
- 扩充 `tests/test_presentation.py`：队列需求、前序数量、阻塞原因、无候选时不生成伪时间、可见性和预测无副作用。
- 运行 `python -m pytest -q`、`node --check app/static/workspace.js` 和 `git diff --check`。
- 浏览器验收完整链路：动态提交、四种 GPU 状态、画像小人、队列展开、表单登记、JSON 上传、发布后大厅可见、重启恢复和撤回。
- 检查桌面与移动宽度、键盘操作及 reduced-motion；确认冻结 `api/openapi.yaml` 无变更。

## 7. 收尾实施步骤与具体对接决定

1. **先合并并行前端成果**：等待正在运行的前端任务结束，逐项审查 `index.html`、`workspace.js`、`workspace.css`。确认 GPU 小电脑按 `offline → self → busy → free` 判定、画像小人保留原画像数据、队列使用新后端字段且能降级、提交页只生成一次有效 Plan/Preview。关键字段使用统一序列化指纹；指纹变化后清空确认摘要并禁止用旧 Plan 提交，正式提交只复用已预估的 `task_id + plan`。
2. **实现贡献资源工作台**：在 `index.html` 的 `mount` 页保留简短说明，新增 `contributionForm` 与 `contributionImportForm` 两张操作卡，以及 `contributionList`、`contributionEvents`、`contributionErrors`。在线表单字段固定为工作站名称、CPU 核数、内存 GiB、临时存储 GiB、GPU 型号、单卡显存和 1–8 张同规格 GPU；提交时分别换算为 millicores/MiB，并生成后端要求的 `gpus` 数组。JSON 卡只接受 `.json`，显示 1 MiB、1–10 节点、每节点 1–8 卡、整批 32 卡限制及内联模板，不增加 CSV 或真实硬件 Agent。
3. **接通贡献状态和操作**：在 `workspace.js` 增加 `state.contributions`，与基础 Dashboard/Profile 并行请求 `/api/demo/contributions`，失败时单独降级且不影响全页。新增 `renderContributions()`、在线登记和上传处理；multipart 请求使用 `FormData`，不手工设置 `Content-Type`，继续携带 `X-Owner` 并保留身份切换防护。展示服务端逐项 `detail` 错误；发布/撤回按钮使用 `data-contribution-action` 和记录 ID，操作期间防重复提交，成功后统一 `await load()` 刷新登记、资源大厅、画像和队列。
4. **明确资源生命周期**：登记卡展示“已登记 / 已发布 / 已撤回”、规格、节点 ID、发布时间和事件；只有 `registered` 显示发布按钮，只有 `published` 显示撤回按钮。发布文案明确“进入 Demo 调度账本，默认个人私有且需卡主审批”，登记成功不宣称已增加容量，撤回 409 原样说明活动 Allocation 限制。更新贡献者使用指南，去掉“尚未实现资源注册”的旧描述，同时保留“非真实硬件探测”的边界。
5. **统一最终视觉**：在前端任务成果上收敛 CSS 变量和覆盖顺序，清除会回退为绿色的旧主题规则；正文保持约 14px、辅助文字不低于 12px，中文标题不使用过度负字距。贡献表单、登记列表、队列时间线、GPU 小电脑和画像小人沿用同一蓝色组件体系，窄屏依次变单列，所有动画只使用 transform/opacity 并受 reduced-motion 控制。
6. **加载 Qwen 配置并重启联调服务**：代码完成后重启 8081/8091 服务，避免旧进程造成版本混淆。若启动终端已提供 `LLM_PROVIDER=qwen`、`LLM_MODEL=Qwen3.8-27B`、Base URL 和 API Key，则验证真实 provider/model/used 状态；未配置时只验收规则回退，不读取或打印 Key，也不虚报模型调用成功。
7. **独立验收**：主执行任务重新运行 `python -m pytest -q`、`node --check app/static/workspace.js`、`git diff --check`，检查 `api/openapi.yaml` 无差异。使用浏览器验证动态字段、建议采纳、预估失效与同方案提交，四种 GPU 状态、画像小人、我的队列摘要和阻塞解释；用临时 JSON 文件完成登记/导入 → 发布 → 大厅可见 → 重启恢复 → 撤回全链路，并验证活动 Allocation 返回 409、跨用户不可见、桌面与移动布局及 reduced-motion。
