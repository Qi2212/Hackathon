# 第三方依赖与素材来源

核验日期：2026-11-15。源码基线：f5aed8076b273b4ac78f575346f45f66756cc2e6

主网页演示使用根目录 requirements.txt；数据库原型位于独立 resource_control_core/，未接入 run.py 主流程。

| 组件 | 版本 | 许可证 | 实际用途 | 官方来源 |
| --- | --- | --- | --- | --- |
| Python | 3.11+ | PSF License 2 | 语言运行时；具体安装版本由复现环境选择 | [许可证](https://docs.python.org/3.11/license.html) |
| FastAPI | 0.115.6 | MIT | A/B HTTP API 与静态文件服务 | [许可证](https://github.com/fastapi/fastapi/blob/0.115.6/LICENSE) |
| Uvicorn standard | 0.34.0 | BSD-3-Clause | ASGI 服务启动与标准扩展 | [许可证](https://github.com/encode/uvicorn/blob/0.34.0/LICENSE.md) |
| Pydantic | 2.10.4 | MIT | 请求与响应模型校验 | [许可证](https://github.com/pydantic/pydantic/blob/v2.10.4/LICENSE) |
| HTTPX | 0.28.1 | BSD-3-Clause | A/B 通信与接口测试 | [许可证](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md) |
| pytest | 8.3.4 | MIT | 自动化测试工具 | [许可证](https://github.com/pytest-dev/pytest/blob/8.3.4/LICENSE) |
| python-dotenv | 1.0.1 | BSD-3-Clause | 读取本机环境配置 | [许可证](https://github.com/theskumar/python-dotenv/blob/v1.0.1/LICENSE) |
| python-multipart | 0.0.20 | Apache-2.0 | 模拟资源 JSON 文件上传解析 | [许可证](https://github.com/Kludex/python-multipart/blob/0.0.20/LICENSE.txt) |
| SQLAlchemy | 2.0.36 | MIT | 仅独立 resource_control_core 原型使用 | [许可证](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_0_36/LICENSE) |
| psycopg binary | 3.2.3 | LGPL v3 | 仅独立原型的可选 PostgreSQL 驱动；二进制附带库另见其许可 | [许可证](https://github.com/psycopg/psycopg/blob/3.2.3/LICENSE.txt) |
| SQLite | 环境内置版本 | Public Domain | 独立原型的默认数据库；主网页演示不使用 | [许可证](https://www.sqlite.org/copyright.html) |

上述为直接依赖清单，不是完整 SBOM；uvicorn[standard] 等传递依赖以安装发行包所附许可证为准。

## 前端与素材

- 前端为原生 HTML / CSS / JavaScript / SVG，未引入 Vue、React、Three.js 或 ECharts。
- logo-mark.png 与 logo-full.png：团队确认由 AI 辅助生成，未提供具体工具或独立素材许可证。
- 3D GPU、图标和曲线在 index.html、workspace.js、workspace.css 中实现；源码存在不等于证明所有元素的独占原创权。
- 资源、用户和历史数据由 app/synthetic.py 合成，项目计划样例在 app/data/experience_plans.json；不使用 Excel 或真实遥测。
- 字体使用本机系统字体，源码包不分发字体文件。
- 使用 Codex 辅助代码和文档编写，结果通过测试与浏览器实测核对。主界面分析为模拟 API，不调用真实大模型。

## 项目整体授权

当前仓库没有项目整体 LICENSE 文件。第三方组件许可证不自动适用于项目整体，本说明未替团队指定开源协议。
