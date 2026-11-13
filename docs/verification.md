# 验证记录

2026-11-13 更新：展示版增加行为准则、阶段匹配、流程星期配置、批量预览与数据库迁移。全部验证使用临时数据库和虚构数据，不读取正式数据库。

```sh
python -m unittest discover -q
python verify_local.py
python portfolio_demo.py
```

本轮 Mac 本地验证：164 项 discover 测试、11 项独立校验和 10 步虚构数据演示全部通过；工具 Schema 导出发现 36 个工具。GitHub Actions 在 Python 3.12 / 3.14 执行以上命令，CI 结果以仓库 Actions 实际记录为准。

历史基线：2026-11-11 Windows 与 Ubuntu / Python 3.12 完成 130 项测试；首次 GitHub Actions 的 Python 3.12 / 3.14 两个任务通过。后续测试数量增加，历史数字不是当前覆盖范围。

正式实例另完成 schema v2 升级、健康与就绪检查、工具发现及代表性聊天验收；这些记录不代表所有自然语言表达都无需澄清。公开仓库不包含正式数据或个人部署记录。
