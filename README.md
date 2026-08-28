# Creator Engagement Project

根据公开内容 URL 获取互动量和一级评论的独立项目。当前部署
运行于 `MyAgent`（Python 3.13.12），采用协议优先、浏览器兜底、代理池复用和企业级重试。

项目当前支持九个渠道：抖音、今日头条、微信（微信公众号）、微信视频号、小红书、好看视频、
快手、哔哩哔哩和微博。微信公众号与微信视频号是两个不同的平台适配器。

## 当前业务边界

默认配置为 `STRICT_ANONYMOUS_MODE=true`，生产流程遵守以下约束：

- 服务本身不执行人工登录、扫码或验证码；小红书游客浏览器可读取首批公开评论，深分页需要
  部署方显式提供已有账号 Cookie。
- 不依赖其他项目或缓存型数据服务；无法从平台实时协议取得的数据会明确返回失败。
- 历史账号 Cookie、公众号凭据、微信侧车和账号 Profile 均被忽略；只有显式启用的
  `XIAOHONGSHU_SESSION_MODE=cookie` 可以注入小红书专用 Cookie。
- 只获取一级评论正文，不获取评论下的回复正文。
- 评论对象中的 `replies` 仅表示平台返回的回复数量，不会触发额外请求。
- “全部评论”指当前会话能顺序访问的全部一级评论；小红书匿名游客通常只开放首批。结果不包含
  删除、隐藏、审核折叠或其他不可见内容。

当前一级评论能力：

| 平台 | 互动量 | 一级评论正文 | 一级评论分页 |
|---|---|---|---|
| 抖音 | 匿名协议可用 | 可用 | 可翻到公开末游标 |
| 今日头条 | 可用，部分文章受 SSR 挑战影响 | 可用 | 可按 offset 翻到公开末页 |
| 微信（微信公众号） | 严格匿名模式不可稳定获取 | 不可用 | 不适用 |
| 微信视频号 | `sph/eid` 匿名可用；客户端 `feedID` 需要授权侧车 | 无账号不可用；授权侧车可用 | 授权侧车按 `lastBuffer` 翻页 |
| 小红书 | 带有效 `xsec_token` 的 URL 可匿名获取 | 匿名首批可用；Cookie 模式可深分页 | 匿名通常仅首批；Cookie 模式按 cursor 翻页 |
| 好看视频 | 可用，未公开字段保持 `null` | 可用 | 可按页翻到公开末页 |
| 快手 | 自动游客状态可用 | 可用 | 可按游标翻到公开末页 |
| 哔哩哔哩 | 视频、专栏和 Opus 可用 | 可用 | 可翻到公开末页 |
| 微博 | 访客公开字段可用 | 可用 | 可翻多页，但不能保证到底 |

小红书匿名游客属于首批公开评论能力；页面要求登录后会停止，不会伪造深分页。微博已经验证
能够获取第二页，但更深页可能触发 `ok=-100`、登录跳转或风控，因此不能承诺全部。

哔哩哔哩页面的评论总数可能包含评论下的回复数量，所以一级评论行数少于 `total_comments`
不一定表示漏页。哔哩哔哩直播不在业务范围内，`live.bilibili.com` 会在请求上游前被拒绝。

## 对外接口

服务提供一个批量接口和两个兼容的单项接口：

```text
POST /api/v1/collect
GET /api/v1/interactions?url=<URL>&media_name=<MEDIA>
GET /api/v1/comments?url=<URL>&media_name=<MEDIA>
GET /api/v1/comments?url=<URL>&media_name=<MEDIA>&page=1
```

另有健康检查：

```text
GET /api/v1/health
```

完整的请求参数、字段定义、错误响应和调用示例见 [API 接口文档](docs/API.md)。服务启动后也
可以访问 `/docs`、`/redoc` 或 `/openapi.json` 查看 FastAPI 自动生成的接口定义。

互动量与评论是两个独立业务请求，分别执行并分别缓存。一个代理 IP 可以在有效期内承载多个
HTTP 请求；复用同一个 IP 不等于把互动量和评论合并成一次上游请求。

`media_name` 的规范值为：

```text
douyin / toutiao / wechat / wechat_channels / xiaohongshu /
haokan / kuaishou / bilibili / weibo
```

接口也接受抖音、头条、微信、微信公众号、微信视频号、小红书、好看、快手、哔哩哔哩、
B站和微博等中文名称。
服务会同时校验 URL 平台和 `media_name`；两者不一致时，单项接口返回 HTTP 422，批量接口
把对应项标为 `failed` 并继续处理其他项。

视频号同时接受公开 `weixin.qq.com/sph/...`、`finder-preview/pages/sph|feed`，以及业务文件中的
`mobile/commonFinderJsApi.html?...extInfo.feedID=export/...`。最后一种链接不公开视频数据，互动量和
评论正文都需要按 [视频号授权侧车文档](docs/WECHAT_CHANNELS_BRIDGE.md) 部署 Windows 微信客户端；
未配置侧车时接口会明确失败，不会使用外部缓存补数。

批量接口接收包含 `url`、中文 `media_name`、`type` 和可选 `page` 的 `items` 数组，不设置
业务条数上限。评论项不传 `page` 时获取全部当前可见一级评论，传数字时只获取对应页。响应
使用规范名“微信”和“哔哩哔哩”，同时继续接受“微信公众号”和“B站”。返回项使用固定字段
结构，平台没有公开的字段为 `null`，并在批次顶层返回总耗时 `duration_ms` 和本批次触发新增
代理 IP 的采购成本 `cost_yuan`。完整示例见接口文档。

### 调用示例

互动量：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/interactions' \
  --data-urlencode 'url=<内容 URL>' \
  --data-urlencode 'media_name=抖音'
```

全部可获取的一级评论：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/comments' \
  --data-urlencode 'url=<内容 URL>' \
  --data-urlencode 'media_name=抖音'
```

只获取一级评论第 1 页：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/comments' \
  --data-urlencode 'url=<内容 URL>' \
  --data-urlencode 'media_name=抖音' \
  --data-urlencode 'page=1'
```

不传 `page` 时，服务从第 1 页顺序获取到当前可见末页、按评论 ID 去重后一次返回。传入
`page=N` 时只获取第 N 页，每页最多 20 条，不包含前面页面。默认全量过程中如果深页触发
平台风控，返回此前已经验证的数据；如果第一页就不可用则返回 HTTP 502。默认全量请求耗时
随评论页数增加，下游需要固定延迟时应显式传入 `page`。

### 响应语义

两个业务接口统一使用最小 `data` 外壳。互动量响应：

```json
{
  "data": {
    "views": 100,
    "likes": 20,
    "comments": 5,
    "shares": 2,
    "favorites": 3,
    "coins": null,
    "danmaku": null,
    "reposts": null,
    "recommendations": null
  }
}
```

评论响应：

```json
{
  "data": [
    {
      "comment_id": "123",
      "author": "用户昵称",
      "text": "一级评论正文",
      "created_at": "2026-08-25T03:20:31Z",
      "likes": 2,
      "replies": 1
    }
  ]
}
```

采集来源、覆盖状态、重试次数和能力说明仍保留在内部结果与日志中，不再要求下游消费。
参数或 URL 不合法返回 HTTP 422；没有任何可用数据、平台拦截或协议失败返回 HTTP 502。

## 安装和启动

项目不使用 Python 3.8。安装和运行统一使用 `MyAgent`：

```bash
cd /home/txy/Agent_first/Creator_Engagement_Project
conda run -n MyAgent python -m pip install '.[browser,test]'
```

创建本地配置：

```bash
mkdir -p .local/env
cp .env.example .local/env/.env
```

启动 API：

```bash
conda run -n MyAgent uvicorn app.api.app:create_app \
  --factory --host 0.0.0.0 --port 8200
```

`0.0.0.0` 是服务端监听地址，调用方不能把它当作目标地址。当前公网调用地址为
`http://39.106.202.228:8200`，同一内网也可以使用 `http://10.0.0.45:8200`。TCP 8200 已
实测可从公网访问；当前接口没有业务鉴权，建议在云安全组中只允许下游服务器的来源 IP。

也可以直接使用 CLI：

```bash
conda run -n MyAgent creator-engagement interactions '<内容 URL>' bilibili
conda run -n MyAgent creator-engagement comments '<内容 URL>' 哔哩哔哩 --page 1
```

增加 `--direct` 可以让单次 CLI 调用绕过代理配置。

## 配置、代理和稳定性

运行配置读取自 `.local/env/.env`，完整模板见 `.env.example`。当前主要配置：

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `STRICT_ANONYMOUS_MODE` | `true` | 禁止复用账号 Cookie、凭据、侧车和历史账号 Profile |
| `XIAOHONGSHU_SESSION_MODE` | `disabled` | 设为 `cookie` 时仅放行小红书专用会话 |
| `XIAOHONGSHU_COOKIE` | 空 | 小红书 Cookie Secret，必须包含非空 `a1` 和 `web_session` |
| `WECHAT_CHANNELS_BRIDGE_URL` | 空 | 授权视频号侧车地址；客户端 `feedID` 评论正文需要配置 |
| `WECHAT_CHANNELS_BRIDGE_TOKEN` | 空 | 视频号侧车鉴权令牌 |
| `PROXY_MODE` | `prefer` | 有代理配置时优先代理，无代理时允许直连 |
| `PROXY_51_API_URL` | 空 | 51 代理供应接口 |
| `PROXY_POOL_SIZE` | `8` | 代理池目标数量 |
| `PROXY_MAX_CONCURRENCY` | `1` | 单个代理的最大并发 |
| `RELIABILITY_MODE` | `enterprise` | 启用协议重试、语义失败换 IP 和平台保护 |
| `PROTOCOL_MAX_ATTEMPTS` | `3` | 企业模式最大协议尝试次数 |
| `TOUTIAO_PROTOCOL_MAX_ATTEMPTS` | `1` | 头条 SSR 能力探测次数；避免确定性空结果重复换 IP |
| `DOUYIN_PROTOCOL_MAX_ATTEMPTS` | `5` | 抖音 HTTP 200 空包时的协议换 IP 上限 |
| `COLLECTION_MAX_CONCURRENCY` | `8` | 全局采集并发 |
| `BROWSER_FALLBACK_ENABLED` | `true` | 协议不可用时允许浏览器兜底 |
| `BROWSER_MAX_CONCURRENCY` | `3` | 浏览器最大并发；每个并发槽使用独立持久化 Profile |
| `BROWSER_MAX_ATTEMPTS` | `3` | 兜底空结果或阻断时换 IP 再试的次数 |
| `BROWSER_GEOIP_ENABLED` | `false` | 是否额外查询代理 GeoIP；默认关闭以避免外部探测超时 |
| `ENGAGEMENT_CACHE_TTL_SECONDS` | `120` | 成功结果缓存时间 |
| `ENGAGEMENT_CACHE_MAX_ENTRIES` | `1000` | 最大缓存项数 |

代理池支持批量补池、IP TTL、单 IP 并发限制、失败淘汰和供应商 API 限流。一次业务调用内部的
预热和数据请求可以共用代理租约，但每个 HTTP 调用仍分别计入上游请求量。

默认企业模式最多进行 3 次协议尝试，头条互动 SSR 探测根据实测单独限制为 1 次，抖音空包最多尝试 5 个协议出口；浏览器兜底最多换 IP 尝试 3 次。小红书游客评论固定直连，避免轮换出口破坏浏览器生成的游客会话。HTTP 200 空包、验证码或缺少目标字段不会被当作成功；
可重试的语义失败会淘汰当前代理。相同接口、相同 URL、相同页码的并发请求会合并，成功或
有效部分结果缓存 120 秒。

浏览器兜底默认限制为单并发。严格匿名模式下，浏览器和游客状态写入：

```text
.local/browser-profiles/anonymous/<platform>
.local/platform-sessions/anonymous/<platform>.json
```

公众号和微信视频号评论不会通过历史账号 Profile 或浏览器登录绕过严格匿名边界。小红书可由
浏览器自动建立游客状态并模拟滚动读取首批公开评论；不会自动登录。显式配置的账号 Cookie
只发送给 `edith.xiaohongshu.com` 评论接口，且不会出现在日志、错误原因或 API 响应中。抖音签名、
哔哩哔哩 WBI 密钥以及快手短期游客状态均在运行时生成，不应硬编码到源码或日志。

视频号客户端 `feedID` 链接的互动量和评论正文都需要把 `STRICT_ANONYMOUS_MODE` 设为 `false`
并配置授权侧车。没有授权客户端时，主服务返回明确失败；主服务不接收微信密码、扫码结果或
Cookie，也不查询其他项目的数据服务。具体客户端路径见
[视频号授权侧车文档](docs/WECHAT_CHANNELS_BRIDGE.md)。

启用小红书评论深分页：

```dotenv
XIAOHONGSHU_SESSION_MODE="cookie"
XIAOHONGSHU_COOKIE="a1=<设备会话>; web_session=<账号会话>; ..."
```

评论 URL 仍须带当前有效的 `xsec_token`。Cookie 失效后接口会返回明确失败状态，服务不会
通过扫码、短信、验证码或浏览器自动登录恢复账号会话。

## 项目结构

```text
app/api/                         HTTP 路由和参数校验
app/core/                        配置与日志
app/models/engagement.py         统一响应模型
app/services/engagement_service.py
                                 缓存、并发、代理和业务服务
app/crawlers/engagement.py       URL 校验、平台路由和浏览器兜底
app/crawlers/comment_capabilities.py
                                 九个平台一级评论能力定义
app/crawlers/platforms/          每个平台独立协议实现
app/crawlers/browser_fallback.py Camoufox 浏览器兜底
app/crawlers/proxy_provider.py   代理池和限流
tests/                           自动化测试
docs/                            流程、协议、成本和测试报告
```

平台文件相互独立：

```text
douyin.py / toutiao.py / wechat.py / wechat_channels.py /
xiaohongshu.py / haokan.py / kuaishou.py / bilibili.py / weibo.py
```

## 验证

当前全量自动化结果：

```text
203 passed
ruff check app tests: All checks passed
```

本地复验：

```bash
conda run -n MyAgent pytest -q
conda run -n MyAgent ruff check app tests
conda run -n MyAgent python -m compileall -q app tests
```

## 详细文档

- [API 接口文档](docs/API.md)
- [九个平台独立执行流程](docs/PLATFORM_FLOWS.md)
- [协议能力矩阵](docs/PROTOCOL_MATRIX.md)
- [浏览器兜底边界](docs/BROWSER_FALLBACK.md)
- [成本、容量与服务器保护](docs/COST_AND_CAPACITY.md)
- [HTML 成本单](docs/ENGAGEMENT_COST_SHEET.html)
- [真实测试报告](docs/TEST_REPORT.md)

历史账号、公众号 Cookie、官方自有号和微信客户端侧车相关文档仅用于兼容代码维护；默认严格
匿名部署不会启用这些能力，也不应把它们计入当前生产成功率。
