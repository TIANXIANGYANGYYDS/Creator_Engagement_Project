# 八个平台独立执行流程

本文对应 `app/crawlers/platforms/` 下的八个实现文件。公共 API 不包含平台条件分支；
`EngagementCrawler` 根据 `media_name + URL` 选择一个处理器，协议结果不可用时再进入统一
Camoufox 兜底。互动量和评论始终是两条独立请求路径：

```text
GET /api/v1/interactions?url=...&media_name=...
  -> include_stats=true, include_comments=false

GET /api/v1/comments?url=...&media_name=...&page=N
  -> include_stats=false, include_comments=true
```

## 抖音

代码：`app/crawlers/platforms/douyin.py`

- 互动量：带调用方抖音 Cookie 请求 `/aweme/v1/web/aweme/detail/`，解析播放、点赞、
  评论、分享、收藏等字段。
- 评论：从 cursor=0 开始请求 `/aweme/v1/web/comment/list/`，按公开 `page` 顺序遍历游标。
- 失败处理：HTTP 200 空包不算成功；标记 `blocked` 后进入目标作品页浏览器兜底。
- 会话边界：不硬编码 Cookie、`a_bogus` 或设备参数；临时状态由调用方会话或浏览器生成。

## 今日头条

代码：`app/crawlers/platforms/toutiao.py`

- 互动量：读取文章 HTML 中的 SSR `itemCounter/likeData`。
- 评论：请求 `/article/v4/tab_comments/`，公开页码转换为 `offset`。
- 失败处理：SSR 没有计数时进入浏览器兜底；评论接口异常时返回明确失败原因。
- 会话边界：当前评论路径无需账号，互动字段可能受页面 JSVM 挑战影响。

## 微信公众号

代码：`app/crawlers/platforms/wechat.py`

- 互动量：先解析文章页 `appmsgstat/cgiDataNew`，有自有文章会话时再尝试
  `/mp/getappmsgext`。
- 评论：先解析页面直接下发的 `preload_comment_list`；首屏有精选评论时无需额外会话。
  否则检查 `show_comment`；作者关闭评论时返回 `complete` 空结果。开启评论时，使用文章
  参数和自有会话请求 `/mp/appmsg_comment`，页码转换为 offset。
- 失败处理：缺参数、`ret=-3 no session` 和平台阻断分别返回 `unsupported/blocked`，随后
  可由浏览器验证当前会话可见内容。
- 会话边界：任意公众号文章不存在已验证的免费匿名互动/评论接口，不伪造手机微信会话。

## 小红书

代码：`app/crawlers/platforms/xiaohongshu.py`

- 互动量：有 Cookie 和 `xsec_token` 时优先签名请求 `/api/sns/web/v1/feed`；否则解析
  笔记 SSR `noteDetailMap`。
- 评论：使用 `xhshow` 为 `/api/sns/web/v2/comment/page` 动态生成完整签名头，按 cursor
  遍历到指定页。旧 `XYS_` 被 HTTP 406 拒绝时自动重试 `XYW_`。
- 详情：`/api/sns/web/v1/feed` 同时生成当前版本需要的 `x-rap-param`。
- 失败处理：无 Cookie 时进入游客浏览器读取公开首屏；游客态不会把第一页冒充后续页。
- 会话边界：首屏可游客访问；稳定深分页需要调用方自己的会话和有效 `xsec_token`。

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
  `photo.id == URL photoId`。
- 评论：优先请求 `/rest/v/photo/comment/list`，必要时回退 GraphQL
  `commentListQuery`，按 `pcursor` 遍历指定页；置顶评论若在后续页重复，会按评论 ID 去重。
- 失败处理：无短期游客状态、Need captcha 或目标 ID 不一致时均不算成功；浏览器打开目标页
  重新生成游客状态后重试。
- URL：同时接受 `www.kuaishou.com/short-video/{id}` 和 `c.kuaishou.com/fw/photo/{id}`；
  分享链接在浏览器阶段自动规范化为同作品详情页。
- 会话边界：公开作品首屏不要求账号；只返回一级评论，不混入推荐流或子回复。

## B 站

代码：`app/crawlers/platforms/bilibili.py`

- 视频互动量：请求 `/x/web-interface/view`，解析播放、点赞、评论、分享、收藏、投币和弹幕。
- 评论：从 `/x/web-interface/nav` 动态提取 WBI key，签名请求
  `/x/v2/reply/wbi/main`，把公开页码转换为内部 cursor；旧 reply 接口仅作兼容回退。
- 直播：`live.bilibili.com/{room_id}` 使用 `Room/get_info` 返回当前在线、关注和开播状态；
  `dM/gethistory` 只返回最近弹幕窗口，不将其伪装为历史评论全集。
- 失败处理：WBI key 轮换、接口限流或数据缺失时进入浏览器兜底。
- 会话边界：当前公开路径无需账号，不保存 WBI 临时密钥。

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
  快手分享链接、微博 base62 短 ID 和 B站直播房间。
- `platforms/common.py` 只包含无平台状态的数值、时间及失败结果转换。
- `engagement.py` 只负责统一接口、路由、HTTP 客户端和是否进入浏览器兜底。
- `browser_fallback.py` 只管理持久化浏览器、目标页操作和网络响应收集，不作为首选协议。
- 任一平台新增字段或修复分页时，应在对应平台文件和测试中完成，不向总路由增加条件分支。
