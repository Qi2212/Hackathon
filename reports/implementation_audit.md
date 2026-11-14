# Aurora 实现核对与 Demo 使用说明

核对日期：2026-11-14。对象：run.py 实际启动的 app/main.py、ext/、Aurora 页面、演示 Excel，以及《Campus GPU Fabric 总体技术与产品设计文档 v0.2》。文档中的建议作为核对依据，不作为本次执行指令。

## 原页面为什么没有数据

原 app/static/app.js 请求 /api/dashboard、/api/tasks/smart-submit、/api/approvals、/api/mount-requests，但这些产品接口已移除。A 保留 /v1/*，B 使用 /ext/v1/*，页面因此收到 404。任务操作也没带必需的 X-Owner；预计按钮只弹固定数字。run.py 原来只启动 A；真正接好 B 的工作台需单独运行 run_ext.py。

本次保留 Aurora，改为加载 live.js，增加 Demo 总览适配、同源扩展网关、固定身份、真实解析/确认/四策略/申请/审批，以及队列和收益展示。run.py 默认同时启动 B，退出时回收子进程。填写 API key 不是解决页面接线的条件：不开 LLM 也能演示。

## 启动与配置

依次执行：

    python -m pip install -r requirements.txt
    python -m tools.build_demo_seed
    python run.py

停止旧进程，重新启动，打开 http://localhost:8080 并 Ctrl+F5。不要双击 HTML 或从 Live Server/file:// 打开。

详细扩展工作台仍在 http://localhost:8090/ui/。不要重复启动占用相同端口的进程。已有 B 时设置 FABRIC_EXT_AUTOSTART=0；自定义端口使用 PORT / EXT_PORT，单独启动 B 时同时设置 FABRIC_BASE_URL 指向 A。

复制 .env.example 为项目根目录 .env，填写：

    LLM_PROVIDER=openai_compatible
    LLM_BASE_URL=服务商提供的兼容API基础地址（通常以/v1结尾）
    LLM_MODEL=服务商模型名称
    LLM_API_KEY=你的密钥

重启后生效。BASE_URL、MODEL 都要填；LLM_PROVIDER=none 使用离线规则。LLM 只负责需求理解，匹配与账本由代码处理。模型失败会回退，实际是否调用可查看 tasks/analyze 返回的 context.llm。密钥不能写入 HTML/JS；.env 已加入忽略规则。TEST_KEY 是时钟等演示控制密钥，与模型密钥不同。

顶栏 X-Owner 默认 u-20260309，可使用 users.user_id 切换。这是本机演示身份，不是正式登录鉴权。状态在内存中，重启恢复快照；现场提交不会写回 Excel。

## 用户需求逐项结论

| 需求 | 实际状态 |
| --- | --- |
| Aurora 页面有数据 | 本次接通；容量和任务状态读取 A，历史数据标注 Excel 快照 |
| 当前任务状态 | 本次接通；按 X-Owner 展示，支持释放、续租、记录 checkpoint、手动 resume |
| 队列、我的位置 | 本次增加；基于真实 Pending、优先级与到达顺序模拟，高亮当前身份 |
| 预计开始/结束 | 本次增加 GPU 条件模拟；假定租约到期释放、不续租、不插队，不保证完成时间 |
| 提交前预测哪队哪格哪卡 | 本次增加预览，不占卡；有可行策略时限定其推荐节点与所属队列，无可行策略时做 default 跨池模拟；未实现组内优先 |
| 任务解析、四策略、申请、审批 | 已有 B 实现，本次接到 Aurora；私有卡由提示的卡主身份审批，申请人再确认提交 |
| 无空闲资源入队后自动运行 | 不完整；A 可返回 Pending 但缺自动派发，B 无空闲时不能生成可提交决策，页面提示限制 |
| 公共/实验室/个人来源 | 本次接入来源、归属、小组、公共/私有标签 |
| 贡献者和平台收益 | 本次显示 Excel 来源/贡献者/平台收益历史；与 B 现场结算分开 |
| 自行创建小组、邀请、退出 | 未实现；本次仅展示 Excel groups / group_members |
| 组内实验室卡公共、个人卡仍私有 | 数据模型已表达；完整服务端成员授权与访问边界未实现 |
| 实验室 → 联盟小组 → 公共大厅优先匹配 | 未实现；matching_tiers 配置不等于已执行策略，四策略也不等于分层权限匹配 |
| 大厅散客提供个人卡 | 有演示节点；真实注册、Agent、硬件自检、心跳和挂载未实现 |
| 模型 API | 有可选解析器；离线规则可用，不把规则解析称为真实模型调用 |

队列模拟只考虑同一节点的兼容 GPU 和租约释放时刻。自用卡、无释放时刻的卡、离线节点不假定将要可用。未计 CPU/内存、审批耗时、续租、临时回收、新插队；无解返回空预测。预测并未与 A 的自动派发闭环。运行中任务显示租约到期时间，不冒充任务完成时间。

## 总体设计文档是否全部实现

没有。README 原“P0–P9 完成”指 B 侧自定义阶段，不等于设计文档第 18 节 Phase 0–6 完成，不代表全面黑盒验收。

| 文档范围 | 核对结论 |
| --- | --- |
| 公共 API、Allocation、Job、基础资源账本 | 已有内存模拟与测试；没有真实 GPU 执行器 |
| 硬约束匹配 | 本次补入 A 申请/恢复的显存、profile、node_selector、CPU/内存/临时存储检查；B 通过节点标签固定推荐节点，不再任意落到另一机器 |
| TTL 到期队列、Pending 自动派发、Job 自动完成 | app/main.py 仍不完整；推进时钟主要处理现有抢占截止逻辑 |
| Demand、checkpoint、resume、NotReady | 部分实现且有回归测试；不能宣称文档 I1–I13 全部通过 |
| 故障注入、限流、定时器 | 仍有骨架；/_test/fault 保存配置不等于真正执行注入 |
| CORA-GPU、公平性、优先级老化、多基线、消融 | 有四策略启发式和配对实验；不是完整文档方案 |
| Task Analyzer、Workload Estimator、Availability | 有规则/可选 LLM、启发式/历史估算、短期可用性基线；没有已训练并校准的完整预测模型 |
| 季度项目规划、30/90 天资源日历 | 未实现完整 CRUD、解析、确认和计划驱动预测 |
| 审批、还价、预约 | B 有流程与窗口校验；无事务性容量预留、完整合同生命周期 |
| 贡献、使用、信用、平台费、补偿 | B 有贡献/使用积分账；贡献自报登记；Excel 收益样本不代表完整现金/积分/信用/补偿机制 |
| User/Provider/Admin 工作台 | 部分页面；真实 Agent、登录与管理权限、定价治理、争议处理缺失 |
| PostgreSQL、Redis、Outbox、持久化、生产部署 | 当前运行路径仍是单机内存 |
| 实验、视频、提交材料 | 有部分报告；未核验完整比赛提交包 |

resource_control_core/ 是另一个实现目录，run.py 不启动它，不能把其中功能自动计入当前页面能力。

## Excel 使用映射

原脚本只读取 nodes、gpus、allocations、params、overview_kpis、labs、queues。本次新增 presentation 分区，展示历史与运行账本分离，不把所有表灌进可写账本。

| 表 | 如何使用、当前接入 |
| --- | --- |
| nodes / gpus | 资源、卡槽、占用、来源与卡主；生成种子后启动时载入 |
| allocations | 非终态注入 A；名称等用于展示；终态历史不重新占卡 |
| params / users / labs | 快照时刻、演示身份参考、实验室画像与积分历史 |
| groups / group_members | 本次小组展示；修改表不会自动授权成员 |
| queues / queue_entries | 队列目录与预制样本；页面排位根据 A Pending 重算条件模拟 |
| submit_predictions / prediction_tier_walk / prediction_candidates | 已导入展示分区，供讲解/后续校准；现场表单不盲套某条预制预测 |
| pricing_rules / matching_tiers | 已导入配置样本；完整关系定价、分层匹配仍未执行 |
| approvals / events / jobs | 历史展示；现场新审批在 B，Job/事件在 A |
| revenue_by_source / revenue_by_contributor / platform_income | 本次来源、贡献者与平台收益页面 |
| revenue_daily / revenue_by_node | 已导入，尚未全部绘图 |
| util_history / lab_daily | 本次利用率历史、实验室热力图与贡献柱图 |
| queue_wait_history | 已导入，尚未参与预测校准 |
| usage_records | 历史审计与未来训练材料；未向页面传全部数千行或导入当前账本 |
| mount_requests | 过程样本，不代表真实 Agent 自检成功 |
| sim_pools / sim_scenarios / sim_conformance | 场景/压测设计材料；不会因表存在自动执行 |
| 说明 / 字段字典 / 缺口清单 / my_revenue | 说明、核对与汇总参考，不按普通数据表导入 |

修改 Excel 后执行 python -m tools.build_demo_seed，再重启 run.py。前端不直接读 Excel。构建自检当前固定 84 卡、8 空闲、1 离线；改变规模需同步调整自检与 tests/test_demo_seed.py，并同步 nodes/gpus/allocations 的关联关系。

## 演示顺序

1. 展示 29 节点 / 84 GPU 和来源标签。
2. 队列页找高亮“我的任务”，解释模拟假设。
3. 提交页点“只做预估”，展示格位、卡槽、时间与四策略，不占卡。
4. 使用当前有空闲兼容卡的配置提交，展示 Allocation/Job 与容量变化。
5. 私有卡按提示切换卡主审批，再切回申请人确认提交。
6. 展示收益拆分与既有小组；说明是预制历史，小组创建/权限仍待开发。
7. 展示事件、释放和续租；不声称全部 TTL/派发/训练完成/鉴权已实现。

## 本次验证

最终完整 pytest 回归：156 passed。使用独立端口实测 A/B 双服务、总览、静态资源、条件预览，以及解析 → 确认 → 四策略 → 创建决策 → 提交 Allocation → Job Running → 我的任务查询 → 释放；推荐节点与实际节点一致。浏览器控制工具连接超时，未完成实际渲染与布局目视检查。JavaScript 使用 node --check 验证语法。

前端脚本另通过真实接口数据驱动的 DOM 桩渲染冒烟：总卡数、资源卡片、队列、小组、收益和扩展健康状态均成功渲染；这不替代浏览器布局目视检查。
私有资源链路也已实测：审批前提交返回 409，卡主批准后由申请人提交，正确绑定推荐节点，最后释放资源。
