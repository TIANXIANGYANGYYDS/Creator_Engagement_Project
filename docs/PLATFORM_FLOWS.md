# 九个平台独立执行流程

本文对应 `app/crawlers/platforms/` 下的九个实现文件。微信公众号和微信视频号各有独立
适配器、URL 路由与能力边界。公共 API 不包含平台条件分支；
`EngagementCrawler` 根据 `media_name + URL` 选择一个处理器，协议结果不可用时再进入统一
Camoufox 兜底。互动量和评论是两条独立接口，也分别执行上游请求：

```text
GET /api/v1/interactions?url=...&media_name=...
  -> include_stats=true, include_comments=false

GET /api/v1/comments?url=...&media_name=...&page=N
  -> include_stats=false, include_comments=true
```

只有接口类型、媒体、URL 和页码都相同的重复请求才会合并任务并命中 120 秒缓存。两个外部
接口不会互相冒充结果；但某个平台为补齐互动中的“评论总数”时，内部可以调用评论总数
端点，其返回的评论正文不会写入互动响应。一个代理 IP 可以承载多次 HTTP 请求；共用代理
租约不等于合并请求。

## 抖音

代码：`app/crawlers/platforms/douyin.py`

- 匿名会话：先打开目标页预热，再向第一方 `ttwid/union/register` 初始化访客标识；不使用
  固定 Cookie，也不要求账号。
- 签名：每个请求生成 107 字符 `msToken`，用纯 Python SM3/RC4 实现即时计算 `a_bogus`。
- 互动量：请求 `/aweme/v1/web/aweme/detail/`，解析播放、点赞、评论、分享、收藏等字段。
- 评论：从 cursor=0 开始请求 `/aweme/v1/web/comment/list/`，按公开 `page` 顺序遍历游标；
  访客实测首屏 20 条、总数 2909。
- 失败处理：HTTP 200 空包不算成功；标记 `blocked` 后进入目标作品页浏览器兜底。
- 会话边界：访客 `ttwid` 只保留在当前请求/客户端生命周期；可选调用方 Cookie 仍只注入
  抖音域，临时状态不会写入源码或日志。

## 今日头条

代码：`app/crawlers/platforms/toutiao.py`

- 互动量：读取文章 HTML 中的 SSR `itemCounter/likeData`。
- 评论：请求 `/article/v4/tab_comments/`，公开页码转换为 `offset`。
- 失败处理：SSR 没有计数时进入浏览器兜底；评论接口异常时返回明确失败原因。
- 会话边界：当前评论路径无需账号，互动字段可能受页面 JSVM 挑战影响。

## 微信公众号

代码：`app/crawlers/platforms/wechat.py`

- 任意公众号生产路径：调用方把完整文章 Cookie Header 写入本机
  `WECHAT_ARTICLE_COOKIE`。适配器从 Cookie、URL 与 `window.cgiDataNew` 合并
  `uin/key/pass_ticket/appmsg_token/wap_sid2` 等参数；整个业务调用复用同一个 Cookie 和
  IP 租约，不启动浏览器、微信客户端或真实账号。
- Cookie-only 互动量：先解析文章页 `appmsgstat/cgiDataNew`，缺计数时再 POST
  `/mp/getappmsgext`；兼容 `appmsg_read_num/appmsg_like_num`、V2 字段和数值 `0`。
- Cookie-only 评论：先解析 `preload_comment_list`，否则请求 `/mp/appmsg_comment`，按
  `buffer/continue_flag` 顺序遍历并转换为外部 `page`；只返回公开精选评论，不承诺后台
  全量留言。

- 自有公众号互动量：配置 AppID/AppSecret 或 access token 后，通过官方 `stable_token`
  获取并缓存令牌；按文章发布日期请求当前 `/datacube/getarticletotaldetail`，用 URL 的
  `mid_idx` 匹配 `msgid`，返回阅读、点赞、在看、分享、留言和收藏。
- 自有公众号评论：请求官方 `/cgi-bin/comment/list`，将外部页码转换为 `begin/count`。
  该接口只对凭据所属公众号和已开通留言权限的文章生效。
- 关闭评论判据：匿名页的 `show_comment=0` 只表示当前页面没有展示评论区；只有成功的
  评论接口响应明确给出 `enabled=0`，才返回 `complete` 空结果并确认文章未开放评论。
- 失败处理：缺会话字段或 `ret=-3/no session` 返回 `unsupported`，不会浪费 IP 轮换；
  Verify HTML、频繁访问或 `/mp/wappoc_appmsgcaptcha` 返回 `blocked`，允许代理池更换出口。
  Cookie/token 的值永不写入 reason 或日志。
- 会话边界：官方凭据不能跨公众号读取第三方文章；IP 只能缓解验证码和频控，不能把弱
  Cookie 提升成完整文章会话。旧桌面微信 sidecar 仅保留兼容代码，当前生产路径默认关闭；
  Cookie-only 配置与边界见 [`WECHAT_COOKIE_PROTOCOL.md`](WECHAT_COOKIE_PROTOCOL.md)。

## 微信视频号

代码：`app/crawlers/platforms/wechat_channels.py`

- URL：独立识别 `weixin.qq.com/sph/{shortUri}`、公开预览 `pages/sph?id=...`，以及带
  `eid/token` 的预览链接；不会再路由到微信公众号适配器。
- 互动量：一次匿名 POST 请求腾讯公开预览端点
  `/finder-preview/api/feed/get_feed_info`，返回页面展示的点赞、评论总数、转发和收藏。
  `万/W/亿/+` 按公开显示值转换为整数；带 `+` 时仅代表可验证下限，因此标记 `partial`。
- 评论：同一个公开预览响应只能取得评论总数，不含评论正文。评论业务接口仍独立执行一次
  上游请求，返回 `unsupported`、准确总数和原因，不把总数伪装成评论行。
- 失败处理：目标分享已失效或响应没有目标 feed 时明确返回 `unsupported`；默认
  `direct/prefer` 绕过代理，不消耗采购 IP，也不启动 Camoufox；显式 `required` 仍尊重
  运维要求使用代理。
- 会话边界：全网可复现的评论正文方案依赖微信视频号客户端及其
  `finderGetCommentList` 会话（objectId、objectNonceId、lastBuffer）。这不是匿名网页请求，
  当前纯服务端实现不注入微信客户端，也不采集或硬编码个人凭据。

## 小红书

代码：`app/crawlers/platforms/xiaohongshu.py`

- 互动量：使用 URL 中当前有效的 `xsec_token` 匿名直访笔记页，缺少时自动补
  `xsec_source=pc_feed`，从 SSR `noteDetailMap` 解析点赞、收藏、评论和分享；严格不携带
  评论 Cookie。真实百测为 1 GET/次、100/100 有数据。
- 评论：使用 `xhshow` 为 `/api/sns/web/v2/comment/page` 动态生成完整签名头，按 cursor
  遍历到指定页。旧 `XYS_` 被 HTTP 406 拒绝时自动重试 `XYW_`。
- 失败处理：缺/过期 token 是 URL 本身的能力边界；匿名 SSR 无目标数据时直接返回明确状态，
  不再启动高成本浏览器或把同一固定出口无意义重试三次。默认 `prefer` 模式绕过已实测不适合
  此路径的代理；只有调用方明确指定 `required` 时才使用代理并按语义失败换出口。
- 会话边界：互动量无需账号；评论仍需要可用 `web_session` 与有效 `xsec_token`。SSR HTML
  没有评论正文，因此互动量和评论必须保持两个独立上游请求。

## 好看视频

代码：`app/crawlers/platforms/haokan.py`

- 互动量：先访问首页建立匿名百度 Cookie，再请求目标页；从目标页 SSR 的 description 和
  `ssr-icon-*` 读取精确播放、点赞、评论数，并校验 SSR URL 确实包含目标 `vid`。
- 评论：同一接口用 `pn` 选择指定公开页，并解析一级评论。
- 失败处理：限流或字段缺失时进入浏览器兜底。
- 会话边界：当前路径纯协议匿名可用；收藏和分享没有公开数字时保持 `null`。

## 快手

代码：`app/crawlers/platforms/kuaishou.py`

- 互动量：同一游客/自有会话请求 GraphQL `visionVideoDetail`，并强制校验返回
  `photo.id == URL photoId`；详情传输或目标匹配异常时解析目标页 `__APOLLO_STATE__`。
  评论总数由独立评论端点补齐，不能把两次 HTTP 请求写成一次。
- 评论：优先请求 `/rest/v/photo/comment/list`，包括 REST 返回验证码时仍尝试 GraphQL
  `commentListQuery`，按 `pcursor` 遍历指定页；置顶评论若在后续页重复，会按评论 ID 去重。
- 失败处理：企业模式最多 3 次，每次使用独立代理租约；网络失败、Need captcha、HTTP 200
  空包和缺少评论总数都会淘汰当前 IP。已取得播放/点赞时，后续评论总数失败不会再丢弃前者。
- 作品状态：详情明确返回 `status=1040/photo=null` 时停止无意义详情重试，继续返回评论端点
  仍能验证的评论总数，并在 `reason` 中标明字段不完整。
- URL：同时接受 `www.kuaishou.com/short-video/{id}` 和 `c.kuaishou.com/fw/photo/{id}`；
  分享链接在浏览器阶段自动规范化为同作品详情页。
- 会话边界：公开作品首屏不要求账号；只返回一级评论，不混入推荐流或子回复。398 条真实
  URL 企业实测互动/评论均为 398/398 有数据，全程零浏览器；互动中 34 条仅剩评论数。

## B 站

代码：`app/crawlers/platforms/bilibili.py`

- 视频互动量：请求 `/x/web-interface/view`，解析播放、点赞、评论、分享、收藏、投币和弹幕。
- 专栏/图文互动量：`/read/cv{id}` 使用 `/x/article/viewinfo`；新版 `/opus/{id}` 解析页面
  `window.__INITIAL_STATE__` 的 `module_stat`。两种 URL 都只需要 1 个匿名 GET。
- 评论：从 `/x/web-interface/nav` 动态提取 WBI key，签名请求
  `/x/v2/reply/wbi/main`。视频使用 `type=1`；专栏使用页面或 URL 确认的 `type=12/oid`。
  排序固定为 `mode=2`（最新评论），按 `pagination_reply.next_offset` 转换页码；旧 reply 接口
  仅作 WBI 风控回退。
- 请求数：视频评论是详情 + nav + WBI 共 3 GET；`cv` 评论已知 oid，只需 nav + WBI 共
  2 GET；Opus 评论需先读页面映射 oid/type，再加 nav + WBI，共 3 GET。
- 会话隔离：Opus 映射页只用于解析公开 SSR，其响应 Cookie 不写入共享协议会话；否则实测
  老专栏一级评论会从 20 条收缩为 3 条并丢失 `next_offset`。
- 失败处理：WBI key 轮换、接口限流或数据缺失时进入浏览器兜底。
- 会话边界：当前公开路径无需账号，不保存 WBI 临时密钥。
- 业务边界：只接受视频和专栏/图文 URL；`live.bilibili.com` 在上游请求前拒绝，不采集直播
  在线人数或弹幕。

## 微博

代码：`app/crawlers/platforms/weibo.py`

- 互动量：请求 `m.weibo.cn/statuses/show`，解析点赞、评论和转发。
- URL：桌面端 `/用户ID/base62短ID` 会先本地转换为数值 MID，再复用移动端公开接口；不会
  再把用户 ID 误判成作品 ID。
- 评论：优先请求 `m.weibo.cn/comments/hotflow`，按 `max_id` 遍历；后续页返回登录跳转时，
  改用 `m.weibo.cn/api/comments/show?id=...&page=N` 匿名页码接口。
- 文本：把评论 HTML 中图片表情的 `alt` 文本保留下来，避免纯表情评论变成空字符串。
- 失败处理：访客限流、折叠或空结果时进入浏览器兜底。
- 会话边界：当前路径无需账号；匿名页码接口和热门流的排序、总数口径不同，均不能证明
  评论全集。

## 共享边界

- `platforms/registry.py` 只负责媒体别名、URL 平台和作品 ID 识别，包括头条 `/i{id}`、
  快手分享链接、微博 base62 短 ID、B 站专栏/Opus URL，以及视频号 sph/eid 分享 URL。
- `platforms/common.py` 只包含无平台状态的数值、时间及失败结果转换。
- `engagement.py` 只负责统一接口、路由、HTTP 客户端和是否进入浏览器兜底。
- `browser_fallback.py` 只管理持久化浏览器、目标页操作和网络响应收集，不作为首选协议。
- 任一平台新增字段或修复分页时，应在对应平台文件和测试中完成，不向总路由增加条件分支。
