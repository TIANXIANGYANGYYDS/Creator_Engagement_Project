# 浏览器兜底

采集顺序固定为：协议请求 -> 结果校验 -> Camoufox 浏览器会话 -> 明确失败。
浏览器不是每次请求的首选，也不会把 Network 面板里复制出的 Cookie、`a_bogus`、
`x-s`、`kww` 等值写进代码。

## 运行时行为

- `EngagementService.from_settings()` 创建一个 `BrowserFallback`，与协议客户端共用
  同一个 51 代理池。
- 每个平台使用独立的 `.local/browser-profiles/<platform>` 持久化 Profile，平台生成的
  `ttwid`、`msToken`、`UIFID`、`x-s` 等状态由浏览器自己维护并自然过期。
- `CREATOR_ENGAGEMENT_COOKIE` 只在启动页面前注入当前调用方拥有的 Cookie，既不打印也不
  持久化到源码；Profile 本身位于 Git 忽略目录。
- 页面加载后监听 `document/xhr/fetch` 响应，优先解析真实 JSON。评论请求没有响应体时，
  结果会标记为“评论字段未返回”，不会返回成功的空评论列表。
- 对支持懒加载的平台会滚动页面，并尝试点击“评论/重试/刷新”按钮；验证码、登录和安全
  验证不会伪造通过，结果会带 `blocked` 和诊断原因。

## 配置

```dotenv
BROWSER_FALLBACK_ENABLED=true
BROWSER_TIMEOUT_SECONDS=35
BROWSER_CHALLENGE_WAIT_SECONDS=5
BROWSER_HEADLESS=true
BROWSER_PROFILE_DIR=".local/browser-profiles"
```

`MyAgent` 环境需要同时具备 `camoufox` Python 包和匹配版本的 Camoufox 浏览器二进制。
无 X11 的 Docker/服务器默认使用 `BROWSER_HEADLESS=true`；需要可视化挑战时，应在带
Xvfb 的运行环境单独配置，不要把人工验证码结果提交到仓库。

## 当前证据边界

抖音已验证：浏览器详情页会产生 `/aweme/v1/web/aweme/detail/` 有效 JSON，统计字段可
直接解析；同一页面的 `/aweme/v1/web/comment/list/` 在无会话时可能 HTTP 200 空包。
这类结果会保留统计并明确说明评论未返回。公众号、快手和部分小红书页面可能停在登录、
安全验证或验证码页，兜底层会报告阻断，不会把推荐流或页面文案当成目标作品数据。
