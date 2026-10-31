# 项目文档索引

## 当前入口

| 主题 | 文档 |
|---|---|
| 调用与响应 | [API](API.md)、[快速调用](API_QUICKSTART.md)、[评论 IO](COMMENTS_API_IO.md) |
| 每个平台的代码、字段、评论限制 | [平台清单](PLATFORM_SUPPORT.md) |
| 历史 20k 前 80 与最近 2000 条缺口 | [覆盖与待办](PLATFORM_BACKLOG.md) |
| 调度、缓存、代理、会话 | [执行流程](PLATFORM_FLOWS.md)、[协议边界](PROTOCOL_MATRIX.md) |
| 最新修复和实网验收 | [2026-11-01 扩展验收](PLATFORM_EXPANSION_2026-11-01.md)、[完整来源 CSV](PLATFORM_COVERAGE_2026-11-01.csv) |
| 最新评论诊断与修复 | [2026-11-01 微博 / B 站修复及外部请求复核](INCIDENT_2026-11-01_COMMENTS.md) |
| 并发/IP 成本 | [成本和容量](COST_AND_CAPACITY.md) |
| 本地测试 | [测试报告](TEST_REPORT.md) |
| 部署和配置 | [部署说明](../DEPLOYMENT.md)、[配置模板](../.env.example) |
| 小红书专用会话 | [会话监控](XIAOHONGSHU_SESSION_MONITOR.md)、[固定出口](XIAOHONGSHU_FIXED_EGRESS_2026-10-12.md) |
| 作者作品业务 | [作者 API](AUTHOR_CONTENT_API.md) |

## 历史证据

`INCIDENT_*`、`*_2026-*`、`incident-*` 等带日期文件用于保留当时的样本、失败、研究和部署证据，不能用其中的平台数量或会话状态替代当前清单。`TEST_REPORT.md` 和 `COST_AND_CAPACITY.md` 的旧章节也保留日期并标明历史口径。

原始导出、HAR、JS、响应、Cookie/代理材料不放在文档目录，保留在 Git 忽略的 `.local/`。不删除仍有运维入口或测试引用的会话/兼容实现。
