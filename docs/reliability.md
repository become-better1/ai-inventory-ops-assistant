# 可靠性设计与证据

## 从失败场景解释设计

| 可能出现的问题 | 当前防护 | 主要验证入口 |
| --- | --- | --- |
| 网络重试造成重复购买和库存增加 | request_key 幂等；同键不同参数拒绝 | test_service.py、test_purchase_corrections.py |
| 同一订单重复导入 | 商家、订单号、行号组成可靠身份；冲突拒绝 | test_orders.py |
| 两个请求同时修改同一记录 | expected_version 校验和事务；写锁协调 | test_orders.py、test_service.py |
| 金额补录造成库存再增加 | 更正原购买记录，库存保持不变 | test_purchase_corrections.py |
| 部分退货和未知金额被错误计算 | 数量约束；只有可靠条件下推算金额 | test_service.py、test_return_corrections.py |
| 撤销覆盖后续修改 | 保存变更快照，执行前检查版本 | test_service.py、test_rooms.py |
| 批量操作范围发生变化 | 预览标识、有效期、服务端保存计划和版本快照 | test_mcp.py、test_quantities.py |
| 模型传入多余参数或文件路径 | strict 参数模型、extra=forbid、字段白名单 | test_mcp.py |
| 损坏备份覆盖正式库 | 清单校验、完整性与模式检查、恢复前安全备份 | test_maintenance.py |
| 恢复时服务仍在使用数据库 | 服务生命周期锁与写锁；检查 WAL 状态 | test_maintenance.py |

幂等键用于识别同一个请求，订单身份用于识别同一个业务事实，两者不能互相替代。没有可靠订单身份时，系统不会把所有同名购买都强行去重。

## 数据模型

五张核心表：items、purchase_records、item_events、consumption_rules、rooms。

三张辅助表：operations 保存操作与幂等信息；operation_changes 保存变更快照；schema_migrations 保存结构版本和建表校验值。

同一物品组使用整数数量与单位。状态或位置不同的部分需要拆组。金额使用最小货币单位整数，币种分别统计；未知金额与 0 元有不同含义。

## 已验证的范围

2026-11-11：Windows 本地环境与 Ubuntu 24.04 / Python 3.12 云端执行了 130 项测试。包含真实 MCP 客户端与 stdio 子进程调用。云端服务重启后健康与就绪接口均返回 200；通过已连接 ChatGPT 完成真实库存查询；备份任务成功运行。

仓库中的 GitHub Actions 配置是后续持续验证入口，不代表已经在 GitHub 运行。测试覆盖业务与协议边界，不代表经过性能、安全审计或生产 SLA 认证。

## 运维视角的下一步

- 同机备份之外，建立定期异地备份和恢复演练；目前只验证过一次本机异地快照下载。
- 增加备份失败通知、容量监控及备份保留策略。
- 配置独立服务账号，减少对 admin 账号的依赖。
- 建立整机重启演练与恢复时间记录；目前只实测了服务重启。
- 若扩大到多用户，重新设计身份隔离、权限和并发数据库架构。

这些是待办事项，不计入已完成功能。
