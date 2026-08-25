# 浏览器兜底

采集顺序固定为：协议请求 -> 结果校验 -> Camoufox 持久化浏览器会话 -> 明确失败。
这是正式的混合执行链路，验收标准是目标 URL 最终返回可验证数据。浏览器不是每次请求的
首选，也不会把 Network 面板里复制出的 Cookie、`a_bogus`、
`x-s`、`kww` 等值写进代码。

## 运行时行为

- `EngagementService.from_settings()` 创建一个 `BrowserFallback`，与协议客户端共用
  同一个 51 代理池。
- 浏览器全局并发由 `BROWSER_MAX_CONCURRENCY` 限制；同一平台另外串行执行，避免多个进程
  同时写一个持久化 Profile。默认最多 1 个浏览器，确认内存充足并压测后才建议设为 2。
- 互动接口和评论接口分别进入浏览器兜底、分别缓存；不会因为两个请求使用同一个浏览器
  Profile 或同一个代理 IP，就把它们记录成一次上游请求。
- 严格匿名模式下每个平台使用独立的 `.local/browser-profiles/anonymous/<platform>` Profile，平台生成的
  `ttwid`、`msToken`、`UIFID`、`x-s` 等状态由浏览器自己维护并自然过期。
- 浏览器退出前把当前 storage-state 以 0600 权限同步到 `.local/platform-sessions/anonymous`，
  后续协议请求只复用游客状态，避免每次都重新启动浏览器。
- 使用代理时启用 Camoufox GeoIP/时区对齐，browser 可选依赖包含免费的 `maxminddb`。
  可丢弃的快手游客压测 Profile 可在 IP 变化时重建设备状态；该开关默认关闭，不会清除
  用户的真实登录会话。
- `CREATOR_ENGAGEMENT_COOKIE` 是兼容 Stock 配置的抖音会话，只注入抖音域名，既不打印也
  不持久化到源码；其他平台复用各自 Profile，避免跨域 Cookie 污染游客设备状态。
- 页面加载后监听 `document/xhr/fetch` 响应，优先解析真实 JSON。评论请求没有响应体时，
  结果会标记为“评论字段未返回”，不会返回成功的空评论列表。
- 互动量请求不会点击或滚动评论区；只有评论接口触发评论面板与分页加载，保持两条 API 的
  页面行为也相互独立。
- 对支持懒加载的平台先尝试打开“评论/重试/刷新”入口，再同时滚动页面与弹层内部的可滚动
  容器，触发真实翻页请求；验证码、登录和安全验证不会伪造通过，结果会带 `blocked` 和
  诊断原因。
- `creator-engagement-login` 只保留历史兼容，不属于严格匿名生产链路。小红书评论使用部署方
  显式提供的 `XIAOHONGSHU_COOKIE`，不会通过该命令启动浏览器登录取 Cookie。

## 配置

```dotenv
BROWSER_FALLBACK_ENABLED=true
BROWSER_TIMEOUT_SECONDS=35
BROWSER_CHALLENGE_WAIT_SECONDS=5
BROWSER_HEADLESS=true
BROWSER_MAX_CONCURRENCY=1
BROWSER_RESET_GUEST_STATE_ON_PROXY_CHANGE=false
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
`visionVideoDetail` 和一级评论 REST 接口，无需用户登录。全量旧 URL 实测表明成功响应
数据可信，但游客态覆盖率仍低，不能写成稳定全覆盖。小红书互动已改为单次匿名 SSR，
不会因协议失败再启动浏览器。评论只在 `XIAOHONGSHU_SESSION_MODE=cookie` 时使用调用方
提供的有效会话走纯协议 cursor 分页，绝不把首屏重复标成后续页。公众号生产路径
明确禁用浏览器兜底，只使用调用方 Cookie + 纯 HTTP；匿名页 `show_comment=0` 不能证明
作者关闭评论，只有评论接口明确返回 `enabled=0` 才能确认。缺会话返回 `unsupported`，
验证页或频控返回 `blocked`。

视频号公开分享互动量不启动浏览器，直接调用公开预览端点。公开页本身只展示评论总数，
点击评论会引导回微信客户端，因此浏览器兜底不会重复打开网页假装能取得评论正文；需要正文
时使用已实现的调用方微信视频号客户端侧车，见
[`WECHAT_CHANNELS_BRIDGE.md`](WECHAT_CHANNELS_BRIDGE.md)。
