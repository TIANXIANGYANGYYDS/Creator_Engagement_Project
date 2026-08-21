# 浏览器兜底

采集顺序固定为：协议请求 -> 结果校验 -> Camoufox 持久化浏览器会话 -> 明确失败。
这是正式的混合执行链路，验收标准是目标 URL 最终返回可验证数据。浏览器不是每次请求的
首选，也不会把 Network 面板里复制出的 Cookie、`a_bogus`、
`x-s`、`kww` 等值写进代码。

## 运行时行为

- `EngagementService.from_settings()` 创建一个 `BrowserFallback`，与协议客户端共用
  同一个 51 代理池。
- 浏览器全局并发由 `BROWSER_MAX_CONCURRENCY` 限制；同一平台另外串行执行，避免多个进程
  同时写一个持久化 Profile。默认最多 2 个浏览器，低内存服务器建议设为 1。
- 互动接口和评论首页先合并为一个内部任务；只有协议结果仍不可用时才启动一次浏览器，
  浏览器返回的统计和评论同时进入 120 秒缓存。
- 每个平台使用独立的 `.local/browser-profiles/<platform>` 持久化 Profile，平台生成的
  `ttwid`、`msToken`、`UIFID`、`x-s` 等状态由浏览器自己维护并自然过期。
- `CREATOR_ENGAGEMENT_COOKIE` 是兼容 Stock 配置的抖音会话，只注入抖音域名，既不打印也
  不持久化到源码；其他平台复用各自 Profile，避免跨域 Cookie 污染游客设备状态。
- 页面加载后监听 `document/xhr/fetch` 响应，优先解析真实 JSON。评论请求没有响应体时，
  结果会标记为“评论字段未返回”，不会返回成功的空评论列表。
- 互动量请求不会点击或滚动评论区；只有评论接口触发评论面板与分页加载，保持两条 API 的
  页面行为也相互独立。
- 对支持懒加载的平台先尝试打开“评论/重试/刷新”入口，再同时滚动页面与弹层内部的可滚动
  容器，触发真实翻页请求；验证码、登录和安全验证不会伪造通过，结果会带 `blocked` 和
  诊断原因。
- `creator-engagement-login` 支持八个平台，也可通过 `--url` 直接打开同平台内容页。人工
  登录完成后，协议适配器读取 storage-state Cookie，浏览器回退复用同平台持久化 Profile。

## 配置

```dotenv
BROWSER_FALLBACK_ENABLED=true
BROWSER_TIMEOUT_SECONDS=35
BROWSER_CHALLENGE_WAIT_SECONDS=5
BROWSER_HEADLESS=true
BROWSER_MAX_CONCURRENCY=2
BROWSER_PROFILE_DIR=".local/browser-profiles"
PLATFORM_SESSION_DIR=".local/platform-sessions"
```

`MyAgent` 环境需要同时具备 `camoufox` Python 包和匹配版本的 Camoufox 浏览器二进制。
无 X11 的 Docker/服务器默认使用 `BROWSER_HEADLESS=true`；需要可视化挑战时，应在带
Xvfb 的运行环境单独配置，不要把人工验证码结果提交到仓库。

## 当前证据边界

抖音匿名纯协议已验证能同时取得 `/aweme/v1/web/aweme/detail/` 和
`/aweme/v1/web/comment/list/`，常规请求不再启动浏览器。协议出现 HTTP 200 空包时仍保留
本兜底；安全验证或验证码页会报告阻断，不会把推荐流或无关页面文案当成目标作品数据。

快手游客页会自动生成 `kwssectoken/kwscode` 等设备状态；项目在同一页面上下文请求目标
`visionVideoDetail` 和一级评论 REST 接口，无需用户登录。小红书游客页可返回首屏评论，
出现“登录查看全部评论”时会停止分页，绝不把首屏重复标成后续页。深分页只能复用调用方
自己的有效会话。公众号文章的 `show_comment=0` 会直接判定为作者关闭评论；其他文章若
没有微信文章短时会话，会明确返回 `unsupported/blocked`。
