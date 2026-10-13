# 小红书每 30 分钟样例检查与飞书告警

2026-10-13 部署。监测入口为 `python -m app.manually_execute_script.monitor_xiaohongshu_session`。

## 实际检查

- 样例是线上来源文件中的原始 URL：`https://www.xiaohongshu.com/discovery/item/6aca066d000000000a0213b8`。
- 关闭结果缓存，检查互动与第一页评论（包括该页可见回复），不要求计数固定为首次看到的数值。
- 原始链接仍经作者核验恢复访问参数，最多扫描 3 页作者列表，整次样例检查最多 5 分钟。
- 所有请求交给现有 Unix socket 的常驻浏览器，使用本机 IP，沿用 15–25 秒间隔与每 10 次额外休息的全局限速。
- 任一真实互动计数可用、评论页可用且解析完整即可通过；合法空评论页也通过。缺失互动字段不会被判作登录失效。
- 不重新运行 892 条批次，不重新启动浏览器，不自动扫码恢复。

## 调度与停止

crontab 每分钟唤起一次命令，仅读取本机浏览器状态。状态文件中的 `next_probe_at` 限制真实样例检查间隔至少 1800 秒；受 cron 分钟粒度影响，实际约 30–31 分钟一次。

进程使用 `flock` 防止重叠，并在网络请求前保存下一次允许检查的时间，进程重启或出错不会立即重跑。浏览器处于失效、风控、等待扫码或停止状态时，不向小红书发送采集探测，但本机状态检查继续，方便检测恢复。

常驻浏览器不可达、登录异常、风控、请求/程序异常会告警；样例缺字段、空数据或单条笔记不可用仅记录 sample_failed，不发卡片。不将网络故障或数据缺失描述为账号登出。只有下一轮真实样例成功，才结束当前故障并允许后续新故障再次通知。

## 飞书卡片

使用本地 `.local/env/.env` 的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_ERROR_RE_CHAT_ID`。密钥由 `SecretStr` 读取，不写入代码、卡片、检查历史或日志。配置和状态文件权限为 0600。

参考 `/home/txy/BanYue/execution_class/feishubot/bot.py` 的 `worker_run`、`send_json_card_to_chat` 和 `get_tenant_access_token`，以及 `core/card_json_builder.py` 的 `get_error_report_card`：先获取应用 tenant token，再通过 `im/v1/messages?receive_id_type=chat_id` 发送 `interactive` 卡片。卡片沿用时间、级别、模块、文件、主机、说明、追踪 ID，并附固定样例和处理指引。

同一故障只成功发送一次。发送失败保留错误码并每 5 分钟重试；故障 UUID 持久化并作为飞书消息 UUID，降低不确定投递后的重复消息风险。不会发送周期性正常通知或制造虚假登录故障。

## 查看运行情况

- `.local/xhs-monitor/status.json`：最近本机检查、样例检查、下一轮时间、当前故障与卡片发送结果。
- `.local/xhs-monitor/probes.jsonl`：真实样例检查历史，只保存计数、结果和错误分类，不保存评论正文或访问 token。
- `.local/xhs-monitor/cron.log`：调度摘要和启动错误。
- `.local/xhs-session/status.json`、`requests.jsonl`：浏览器会话与脱敏上游请求记录。

删除 crontab 中 `app.manually_execute_script.monitor_xiaohongshu_session` 对应行即可停用监测，不影响现有浏览器及 API。

## 部署验收

首轮 2026-10-13 17:30:14 开始、17:30:59 完成，互动返回点赞 2、评论数 1，第一页取得 1 条正文且解析完整，会话保持 `ready`。

飞书应用令牌获取和机器人信息查询成功。群成员查询接口返回权限不足（`99991672`），但不影响已验证的消息发送：2026-10-13 17:39:47 按用户要求向指定群发送了「【测试】小红书登录验证失效」卡片，接口确认成功并返回消息 ID，凭据脱敏的回执保存在 `.local/analysis/xhs-session-monitor-20261013/feishu-test-card-receipt.json`。测试未修改登录状态或监测故障记录。发送协议、失败留存、去重及重试已通过模拟测试。真实告警是否投递以 `incident.sent` 与 `message_id` 为准；失败以 `incident.delivery_error` 为准。

官方接口参考：[获取应用令牌](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)。

其他平台与接口的状态错误通知见 [平台接口状态告警](STATUS_ALERTS.md)。
