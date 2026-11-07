# 项目文档索引

## 当前入口

| 主题 | 文档 |
|---|---|
| 调用与响应 | [API](API.md)、[快速调用](API_QUICKSTART.md)、[评论 IO](COMMENTS_API_IO.md) |
| 每个平台的代码、字段、评论限制 | [平台清单](PLATFORM_SUPPORT.md) |
| 历史 20k 前 80 与最近 2000 条缺口 | [覆盖与待办](PLATFORM_BACKLOG.md) |
| 调度、缓存、代理、验收规则 | [执行流程](PLATFORM_FLOWS.md) |
| 浏览器兜底 | [浏览器流程与限制](BROWSER_FALLBACK.md) |
| 最新修复和实网验收 | [2026-11-01 扩展验收](PLATFORM_EXPANSION_2026-11-01.md)、[完整来源 CSV](PLATFORM_COVERAGE_2026-11-01.csv) |
| 最新评论诊断与修复 | [2026-11-01 微博 / B 站修复及外部请求复核](INCIDENT_2026-11-01_COMMENTS.md) |
| 并发/IP 成本 | [成本和容量](COST_AND_CAPACITY.md) |
| 本地测试 | [运行与验证命令](../README.md#安装运行和验证) |
| 部署和配置 | [部署说明](../DEPLOYMENT.md)、[配置模板](../.env.example) |
| 状态告警 | [告警配置与排查](STATUS_ALERTS.md) |
| 小红书专用会话 | [会话监控](XIAOHONGSHU_SESSION_MONITOR.md)、[固定出口](XIAOHONGSHU_FIXED_EGRESS_2026-10-12.md) |
| 微信公众号会话 | [Cookie 协议](WECHAT_COOKIE_PROTOCOL.md)、[会话桥接](WECHAT_SESSION_BRIDGE.md) |
| 微信视频号侧车 | [侧车配置与补丁](WECHAT_CHANNELS_BRIDGE.md) |
| 作者作品业务 | [作者 API](AUTHOR_CONTENT_API.md)、[访客模式 v7 验收](AUTHOR_AUTO_SESSION_2026-09-19.md) |

## 文档维护

仓库保留接口规范、平台清单、运维说明和仍有参考价值的验收记录。已被替代的阶段报告、重复说明和成本单导出件移至本机 `.local/document-archive/`，不纳入 Git；后续同类材料也优先保留本地。

保留的验收记录和成本实验以各自日期及样本范围为准，不能用历史平台数量、会话状态或测试通过数替代当前验证。原始导出、HAR、JS、响应、Cookie/代理材料保留在 Git 忽略的 `.local/`。
