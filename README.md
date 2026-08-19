# Creator Engagement Project

独立的协议优先 URL 互动量和公开评论采集项目，与 `Stock_Project` 无代码依赖。
当前运行环境为 `MyAgent`（Python 3.13）。项目采用 Stock_Project 同类的
`app/core`、`app/crawlers`、`app/services`、`app/api`、`app/models`、`tests`
分层；Mongo、调度器和 worker 目录先作为后续持久化/订阅功能的扩展位。

## 运行

在项目根目录执行：

```bash
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement interactions '<内容 URL>' bilibili
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement comments '<内容 URL>' B站 --page 1
```

可选的调用方 Cookie 通过 `CREATOR_ENGAGEMENT_COOKIE` 环境变量注入，不会写入输出；为兼容
Stock_Project 现有环境，也接受 `DOUYIN_SESSION_COOKIE` 作为回退变量名。

启动 API：

```bash
conda run -n MyAgent uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8200
```

## 配置和代理池

项目从 `.local/env/.env` 读取部署配置；`.env.example` 已列出 Stock_Project 中可复用的
LLM、Mongo、51 代理 API 和日志参数。当前代理模式：

- `PROXY_MODE=direct`：只使用本机直连。
- `PROXY_MODE=prefer`：配置 51 代理时使用 `AsyncDailiProxyPool`，没有代理时允许直连。
- `PROXY_MODE=required`：必须取得代理，否则请求直接失败，不会静默直连。

51 代理池沿用 Stock_Project 的 3 分钟 IP TTL、批量补池、单 IP 并发上限、失败淘汰、
过期排空和供应商 API 限流机制。互动采集请求通过 `CurlAsyncHttpClient` 统一租约和归还代理。

协议返回 `unsupported/blocked/failed`、空响应或缺少目标字段时，如果
`BROWSER_FALLBACK_ENABLED=true`，服务会使用 MyAgent 中的 Camoufox 持久化浏览器重新
访问页面并监听真实 `document/xhr/fetch` 响应。每个平台的 Profile 位于
`.local/browser-profiles/<platform>`，动态 Cookie 和签名由浏览器生成，不会硬编码。
实现细节和挑战边界见 [`docs/BROWSER_FALLBACK.md`](docs/BROWSER_FALLBACK.md)。

接口：

- `GET /api/v1/health`
- `GET /api/v1/interactions?url=<URL>&media_name=<MEDIA>`
- `GET /api/v1/comments?url=<URL>&media_name=<MEDIA>&page=1`

`media_name` 的规范值为 `douyin / toutiao / wechat / xiaohongshu / haokan /
kuaishou / bilibili / weibo`，也接受抖音、头条、公众号/微信、小红书、好看、
快手、B站、微博等中文名称。服务会同时识别 URL 所属平台；如果 URL 与
`media_name` 不一致，接口返回 HTTP 422，不会把请求转给错误的平台适配器。

## 能力范围

互动量接口返回 `platform / work_id / stats / coverage / reason`；评论接口返回
`platform / work_id / page / comments / next_page / total_comments / coverage / reason`。
评论页固定最多 20 条。`coverage`
不是装饰字段：`complete` 表示当前接口可完整说明本页，`partial` 表示只能拿到公开可见
部分，`blocked` 表示平台拒绝请求或进入验证码/登录挑战，`unsupported` 表示协议和浏览器
都没有捕获到可验证的目标数据。

| 平台 | 当前能力 | 协议证据 | 状态 |
|---|---|---|---|
| B 站 | 播放、点赞、评论、分享、收藏、投币、弹幕和一级评论 | `x/web-interface/view`、`x/v2/reply/wbi/main`、`x/web-interface/nav` | 可用；评论仅当前页 |
| 微博 | 点赞、评论、转发和热门评论 | `statuses/show`、`comments/hotflow` | 可用，访客态部分覆盖 |
| 好看 | 评论总数和一级评论 | `haokan/ui-web/v2/comment/get` | 可用；详情互动量待补 |
| 小红书 | 点赞、收藏、分享、评论总数 | 详情页 `noteDetailMap` SSR | 可用；评论列表需 `x-s/x-t` |
| 抖音 | 协议优先，浏览器会话可捕获详情统计；评论按真实响应判定 | `/aweme/v1/web/aweme/detail/`、`/aweme/v1/web/comment/list/` | 详情已 smoke 验证；匿名评论可能 HTTP 200 空包 |
| 头条 | 文章 SSR 统计（若首包可解析）、评论总数和一级评论 | `article SSR itemCounter/likeData`、`article/v4/tab_comments` | 评论可用；互动统计受 JSVM/挑战影响 |
| 公众号 | 协议失败后浏览器尝试正文、互动和评论响应 | `/mp/getappmsgext`、`/mp/appmsg_comment`、页面 SSR | 文章会话或登录挑战时明确 blocked |
| 快手 | 协议失败后浏览器尝试 GraphQL 详情和评论响应 | `visionShortVideoReco`、`visionVideoDetail`、`visionCommentList` | 目标校验和验证码失败时明确 blocked，不使用推荐流冒充目标 |

不要把浏览器抓到的临时 Cookie、`x-s`、`hk_sign` 或其他签名硬编码到服务代码。抖音详情接口在当前版本对匿名请求稳定返回 HTTP 200 空包；需要读取统计时，通过 `CREATOR_ENGAGEMENT_COOKIE` 注入调用方自己的会话 Cookie。该 Cookie 不会写入代码或日志，过期、无效或缺少设备风控字段时结果会明确标成 `blocked`/`failed`。B 站评论的 WBI 密钥每次从公开导航接口动态读取，不依赖浏览器或登录 Cookie。

逐项证据和“无法稳定获取”的阻断原因见 [`docs/PROTOCOL_MATRIX.md`](docs/PROTOCOL_MATRIX.md)。

头条评论接口已验证：`/article/v4/tab_comments/` 使用 `aid/app_name/offset/count/group_id/item_id` 即可返回 `err_no=0`、`total_number`、`has_more` 和评论列表，不需要把浏览器请求里的 `_signature` 写入代码。

快手不能用“GraphQL 返回 HTTP 200”作为成功判据。协议层匿名调用 `visionShortVideoReco` 时，返回列表可能完全不包含传入的 `photoId`，它本质上会退化成推荐流；`visionCommentList` 则稳定返回 `Need captcha`。浏览器有效请求还带有 webWeapon 生成的 `kww` 头以及 `kwfv1/kwssectoken` 等短期状态。浏览器兜底会复用按平台隔离的 Profile，但仍会校验目标 ID，不会把推荐流第一条伪装成目标 URL 的统计。

小红书评论接口已经定位到 `api/sns/web/v2/comment/page`，动态脚本也能在 jsdom 中生成 `x-s/x-t/x-s-common`。当前纯协议生成的签名仍被服务端以 HTTP 406 拒绝，因此协议层只返回 SSR 统计；浏览器兜底会用运行时会话尝试触发评论请求，不把浏览器样本签名写进业务代码。
