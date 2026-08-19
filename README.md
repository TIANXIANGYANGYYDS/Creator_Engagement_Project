# Creator Engagement Project

独立的纯协议 URL 互动量和公开评论采集项目，与 `Stock_Project` 无代码依赖。
当前运行环境为 `MyAgent`（Python 3.13）。项目采用 Stock_Project 同类的
`app/core`、`app/crawlers`、`app/services`、`app/api`、`app/models`、`tests`
分层；Mongo、调度器和 worker 目录先作为后续持久化/订阅功能的扩展位。

## 运行

在项目根目录执行：

```bash
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement '<内容 URL>' --comment-limit 20
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

接口：

- `GET /api/v1/health`
- `GET /api/v1/engagement?url=<URL>&comment_limit=20`
- `POST /api/v1/engagement/batch`，请求体 `{ "urls": [...], "comment_limit": 20, "concurrency": 4 }`

## 能力范围

返回字段统一为 `platform / work_id / stats / comments / coverage / reason`。`coverage`
不是装饰字段：`complete` 表示当前接口可完整说明本页，`partial` 表示只能拿到公开可见
部分，`blocked` 表示平台拒绝请求，`unsupported` 表示当前尚未形成无浏览器稳定协议。

| 平台 | 当前能力 | 协议证据 | 状态 |
|---|---|---|---|
| B 站 | 播放、点赞、评论、分享、收藏、投币、弹幕和一级评论 | `x/web-interface/view`、`x/v2/reply` | 可用；评论仅当前页 |
| 微博 | 点赞、评论、转发和热门评论 | `statuses/show`、`comments/hotflow` | 可用，访客态部分覆盖 |
| 好看 | 评论总数和一级评论 | `haokan/ui-web/v2/comment/get` | 可用；详情互动量待补 |
| 小红书 | 点赞、收藏、分享、评论总数 | 详情页 `noteDetailMap` SSR | 可用；评论列表需 `x-s/x-t` |
| 抖音 | 在调用方提供有效会话 Cookie 时读取详情互动量；评论接口可能返回空包 | `/aweme/v1/web/aweme/detail/`、`/aweme/v1/web/comment/list/` | 统计可尝试；匿名请求明确 unsupported，评论按实际响应 partial |
| 头条 | 评论总数和一级评论 | `article/v4/tab_comments`，无需固化 `_signature` | 可用；点赞/转发详情待补 |
| 公众号 | URL/文章 ID 识别 | 正文可获取；互动/评论被文章会话和验证码保护 | 未稳定 |
| 快手 | URL/作品 ID 识别 | `visionShortVideoReco`、`visionCommentList` 已定位 | 未稳定；详情依赖 webWeapon `kww`，评论返回 `Need captcha` |

不要把浏览器抓到的临时 Cookie、`x-s`、`hk_sign` 或其他签名硬编码到服务代码。抖音详情接口在当前版本对匿名请求稳定返回 HTTP 200 空包；需要读取统计时，通过 `CREATOR_ENGAGEMENT_COOKIE` 注入调用方自己的会话 Cookie。该 Cookie 不会写入代码或日志，过期、无效或缺少设备风控字段时结果会明确标成 `blocked`/`failed`。

头条评论接口已验证：`/article/v4/tab_comments/` 使用 `aid/app_name/offset/count/group_id/item_id` 即可返回 `err_no=0`、`total_number`、`has_more` 和评论列表，不需要把浏览器请求里的 `_signature` 写入代码。

快手不能用“GraphQL 返回 HTTP 200”作为成功判据。匿名调用 `visionShortVideoReco` 时，返回列表可能完全不包含传入的 `photoId`，它本质上会退化成推荐流；`visionCommentList` 则稳定返回 `Need captcha`。浏览器有效请求还带有 webWeapon 生成的 `kww` 头以及 `kwfv1/kwssectoken` 等短期状态。当前实现不会复用浏览器状态，也不会把推荐流第一条伪装成目标 URL 的统计。

小红书评论接口已经定位到 `api/sns/web/v2/comment/page`，动态脚本也能在 jsdom 中生成 `x-s/x-t/x-s-common`。当前 Node 环境生成的签名仍被服务端以 HTTP 406 拒绝，说明环境指纹尚未达到可部署标准，因此模块只返回 SSR 统计，不把浏览器样本签名写进业务代码。
