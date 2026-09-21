# 自动 Cookie 与作者内容采集验收（v7，2026-09-19）

## 当前条件与结果

业务参数仍为 `author_id`、`media_name`。没有账号、验证码接收或扫码配置，程序自动取得平台下发的访客 Cookie，不导入本机历史账号 Cookie。Cookie 不等于账号登录凭据；获取到 Cookie 或接口 HTTP 200 都不能证明作品列表可用。

本轮有效改进是 B 站公开投稿请求：在当前代理取得新的访客设备标识和 WBI 密钥，补齐 Web 来源及设备参数后签名；无浏览器采样时使用空 DOM 数据。后续页保留同一租约的会话，受限时沿用 v6 的持久化游标重试流程。未新增作品总数、作者总数或代理消耗次数限制。

| 验证对象 | 真实结果 | 范围说明 |
| --- | --- | --- |
| B 站 `1804532025` | 6 页，各 30 条，共 180 个唯一视频 | 抽检窗口结束后保留第 7 页游标；不是全历史结束 |
| B 站 `1187120568` | 视频 5 页，30/30/30/30/23，共 143 条；第 6 次读取核验专栏数为 0 | 视频投稿来源走到末页；下一来源为动态，不能称作者全集完成 |
| 两位 B 站作者首尾页详情 | 8/8 成功，取得标题、简介、时长及作品链接 | 详情通过独立代理租约获取；未将全部列表数当作已取得详情数 |
| 抖音 `ChenNan12.24` | 自动取得 128 字符的服务端 msToken，首屏 20 条；第二页缺少列表 | 自动令牌与首页预热没有解除分页限制 |
| 抖音 `46219502311` | 自动取得 128 字符的服务端 msToken，首屏 23 条；第二页缺少列表 | 作品归属经过平台公开作者标识核验 |
| 微博 `7002783993` | 访客协议和全新浏览器各能取首屏 10 条，第二页均失败 | `since_id`、简化参数、浏览器 Cookie 初始化均未改善；桌面入口 HTTP 403 |
| 微博 `7727451889` | 个人信息可读，作品列表返回登录提示 | 不是空账号证明 |
| 小红书 | 全新浏览器产生 a1、webId 等 Cookie；三次初始化未取得 web_session，主页返回登录页 | 没有通过真实列表验收 |
| 快手、今日头条 | 本轮没有新的成功结论 | 延续 [v6 实测](AUTHOR_GUEST_2026-09-19.md)：快手列表仍受限；头条一个作者 245 条视频详情完成，另一个作者已抽检 10 篇文章正文 |

B 站两次列表验证仍分别发生 8 次、7 次页级失败后恢复，常见响应为 `-352`、HTTP 412。它证明了多页进度可以推进，不表示每次请求都稳定成功。

## 联网参考与采用边界

- [RSSHub 的 B 站投稿路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/bilibili/video.ts)和[共享请求代码](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/bilibili/utils.ts)：参考公开 Web 参数和同一会话的 WBI 请求；本项目使用空 DOM 数据，未照搬随机鼠标坐标。实际参数经两位作者分页验证后接入。
- [TikTokDownloader 的 msToken 获取](https://github.com/JoeanAmier/TikTokDownloader/blob/master/src/encrypt/msToken.py)：在隔离探针中验证服务端令牌获取。令牌成功下发，但两位真实作者后续页仍失败，因此未把这条额外请求加入生产主路径。
- [TikTokDownloader issue #706](https://github.com/JoeanAmier/TikTokDownloader/issues/706)：上游用户也报告首屏成功、第二页仅返回状态码的现象。这是相关故障报告，不是已解决方案。
- [Douyin Download API 会话与代理说明](https://douyin.wtf/identities-and-proxies/)：参考会话、UA、代理出口的一致性；其 TikTok 令牌长度检查不能直接当作抖音的有效会话判断。
- [MediaCrawler 抖音登录代码](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/login.py)及[小红书登录代码](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/login.py)：二维码、手机验证码、已有 Cookie 都需要账号条件；不能据此声称无需账号就能自动生成登录凭据。
- [mcp-server-weibo](https://github.com/Panniantong/mcp-server-weibo)：核查访客 SUB/SUBP、个人页 containerid 与 since_id 翻页。实际作者响应仍返回 page 而非 since_id，替换为这些参数未解决第二页问题，因此未加入无效分支。
- [ReaJason/xhs 的 activate 实现](https://github.com/ReaJason/xhs/blob/master/xhs/core.py)：补测游客激活入口 `/api/sns/web/v1/login/activate`，三次全新会话及 XYS/XYW 两种签名均连接失败；另一轮浏览器原生调用也失败，没有取得可用 web_session，未加入生产路径。

## 验证与证据

`CREATOR_RUN_MONGO_TESTS=1 /home/txy/miniconda3/envs/MyAgent/bin/python -m pytest -q`：**345 passed**。回归包含完整 Web 查询签名、末页进入专栏、每个代理租约重新初始化访客设备，以及现有任务恢复、身份核验和并发隔离测试。

本机 `.local/analysis/auto-sessions/` 保存列表与详情实测、失败探针和上游源码参考。`bili-verification.json`、`bili-second-verification.json`、`bili-detail-verification.json` 对应上表；`dy-more-results.json`、`wb-since-results.json`、`wb-params-results.json`、`wb-browser-results.json` 记录其他路径。Cookie 值不写入探针报告。

当前仍未实现六个平台无账号条件下的稳定全历史采集。生产响应继续校验作者归属、实际作品字段和分页状态；缺少列表、连接失败、明确登录提示均不会被当作完整空结果。

## 部署后 HTTP 验收

2026-09-19 23:12:07（Asia/Shanghai）已切换 8200 服务，PID `2629268`，健康检查 `author_pipeline_version=7`。切换前活动任务数为 0，回滚快照为 `.local/releases/20260919-231206-author-auto-session`。

两位 B 站作者通过真实批量任务接口提交，HTTP 202 在约 8 毫秒返回。五分钟验收窗口内：

| author_id | job_id | 保存且详情完成 | 已读来源页 | 状态说明 |
| --- | --- | --- | --- | --- |
| `1187120568` | `author_543cf03aab654a1a876b97890473692e` | 143 条 | 7 页（5 视频、1 空专栏、1 动态） | 动态继续分页仍受限，discovery_done=false |
| `1804532025` | `author_51a8d530aa404e839f4b290f4d4011b5` | 180 条 | 6 页视频 | 尚未走到视频来源末页，discovery_done=false |

两任务各经历 7 次任务级自动重试。验收窗口结束后主动取消测试任务，状态为 cancelled；这是测试主动停止，不是平台全集结束。返回结果逐条核验：分别 143/180 个唯一 ID，全部作者归属正确、`content_complete=true`，详情失败和待处理数均为 0。结果及游标保留，可通过 resume 继续。验收结束后活动作者任务和互动任务均为 0。

部署证据见本机 `.local/analysis/auto-sessions/deployment.json`、`deployed-verification.json`、`deployed-results-1187120568.json`、`deployed-results-1804532025.json`。生产版本不包含新增的抖音令牌请求或小红书激活请求，因为这些试验未改善真实列表访问。
