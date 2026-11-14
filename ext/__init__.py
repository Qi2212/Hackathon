"""B 侧扩展服务：智能决策与产品业务层。

本包只通过 A 的冻结 HTTP API 读取 ResourceSnapshot，输出 TaskSpec /
StrategyPlan / PlacementDecision / Approval 等结构化结果，不维护可写资源账本。
"""

__version__ = "0.1.0"
