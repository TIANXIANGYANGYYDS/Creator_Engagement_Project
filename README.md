# Creator Engagement Project

独立的协议优先 URL 互动量和公开评论采集项目，与 `Stock_Project` 无代码依赖。
当前运行环境为 `MyAgent`（Python 3.13）。项目采用 Stock_Project 同类的
`app/core`、`app/crawlers`、`app/services`、`app/api`、`app/models`、`tests`
分层；Mongo、调度器和 worker 目录先作为后续持久化/订阅功能的扩展位。

## 项目结构

统一接口和八个平台实现已经分开。调用链保持为：

```text
app/api                 HTTP 参数和响应
  -> app/services       业务服务
  -> app/crawlers/engagement.py
                         URL 校验、平台路由、浏览器兜底判定
  -> app/crawlers/platforms/
       douyin.py         抖音协议流程
       toutiao.py        头条协议流程
       wechat.py         公众号协议流程
       xiaohongshu.py    小红书协议和签名流程
       haokan.py         好看协议流程
       kuaishou.py       快手协议流程
       bilibili.py       B 站协议和 WBI 签名流程
       weibo.py          微博协议流程
       registry.py       媒体名称和 URL 识别
       common.py         数值、时间和失败结果等无平台状态工具
  -> app/crawlers/browser_fallback.py
                         协议失败后的统一浏览器生命周期和响应监听
```

新增或调整某个平台时，只修改对应的 `platforms/<media>.py`；统一 API、服务层和其他
平台不需要跟着变化。八个平台各自的互动量、评论、分页、会话和兜底流程见
[`docs/PLATFORM_FLOWS.md`](docs/PLATFORM_FLOWS.md)。

## 运行

首次使用时，把当前项目安装到 `MyAgent`，这样模块和两个命令行入口使用的是同一份依赖：

```bash
conda run -n MyAgent python -m pip install '.[browser]'
```

随后可在项目根目录执行模块入口：

```bash
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement interactions '<内容 URL>' bilibili
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement comments '<内容 URL>' B站 --page 1
```

也可以使用安装后生成的 `creator-engagement` 命令；若代码有更新，重新执行上述安装命令：

```bash
conda run -n MyAgent creator-engagement interactions '<内容 URL>' bilibili
```

抖音默认会自行初始化第一方访客 `ttwid`、生成随机 `msToken` 并用纯 Python 计算
`a_bogus`，不要求登录。可选的调用方 Cookie 仍可通过 `CREATOR_ENGAGEMENT_COOKIE`
注入；为兼容 Stock_Project 现有环境，也接受 `DOUYIN_SESSION_COOKIE` 作为回退变量名。
该 Cookie 只会注入抖音域名，不会污染快手、小红书等平台的游客会话。

快手公开作品默认复用本地游客设备状态走纯协议详情和评论接口，状态失效时才由浏览器重新
生成，不需要账号。小红书游客态曾在新鲜
公开笔记读取到互动量和首屏评论，但当前重复实测并不稳定。项目不接入付费数据供应商；
小红书稳定首屏/深分页和任意公众号文章的互动/评论需要时复用调用方自己的平台会话。八个平台都可以在
带桌面环境的本机建立独立 Profile：

```bash
conda run -n MyAgent creator-engagement-login <platform>
```

`platform` 可选 `douyin / toutiao / wechat / xiaohongshu / haokan / kuaishou /
bilibili / weibo`。需要在具体内容页完成登录或安全验证时传入同平台 URL：

```bash
conda run -n MyAgent creator-engagement-login xiaohongshu --url '<小红书笔记 URL>'
```

登录完成后按 Enter，状态只写入 `.local/browser-profiles/<platform>` 和
`.local/platform-sessions/<platform>.json`。公众号还要求文章本身开启评论；微信网页会话
不能保证等价于手机微信文章会话，接口会如实返回 `blocked/unsupported`。

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
过期排空和供应商 API 限流机制。一次业务接口内部的预热和数据请求可共享代理租约，但每个
HTTP 调用仍单独计入上游请求数；互动量接口和评论接口分别执行、分别缓存。

默认启用 `RELIABILITY_MODE=enterprise`：最多 3 次协议尝试，HTTP 200 但业务空包/验证码
也会淘汰当前代理；失败结果不写入 120 秒缓存。服务最多同时运行 4 个采集任务、1 个浏览器
任务，代理池维护 4 个 IP 且每个 IP 单并发。小红书、快手和公众号另有平台级串行与启动
间隔，避免全局 4 并发直接压到单个平台。相同接口、相同 URL 和相同页码的并发重复请求会
合并，成功或有效部分结果缓存 120 秒（最多 1000 项）。配置项、真实内存测试和 51 代理成本公式见
[`docs/COST_AND_CAPACITY.md`](docs/COST_AND_CAPACITY.md)。

运行时以最终获取数据为验收标准：先走成本更低的协议请求；协议返回
`unsupported/blocked/failed`、空响应或缺少目标字段时，如果
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
| B 站 | 视频互动/评论；直播当前状态和最近弹幕 | 视频 `x/web-interface/view`、`x/v2/reply/wbi/main`；直播 `Room/get_info`、`dM/gethistory` | 视频支持分页；直播不提供历史弹幕全集 |
| 微博 | 点赞、评论、转发和匿名评论分页 | `statuses/show`、`comments/hotflow`、`api/comments/show` | 可用，访客态部分覆盖 |
| 好看 | 播放、点赞、评论总数和一级评论 | 目标页 SSR、`haokan/ui-web/v2/comment/get` | 纯协议匿名可用；收藏/分享未公开 |
| 小红书 | 点赞、收藏、分享、评论总数和一级评论 | 签名 feed/评论接口、SSR、浏览器兜底 | 低频评论最新 20/20；互动最新 0/20 且代理+浏览器仍验证码，稳定互动需要更有效的调用方会话 |
| 抖音 | 匿名纯协议互动量和评论分页；浏览器仅作风控兜底 | 第一方访客 `ttwid`、纯 Python `a_bogus`、`/aweme/v1/web/aweme/detail/`、`/aweme/v1/web/comment/list/` | 真实首屏 20 条、总数 2909；平台隐藏 `play_count` 时播放保持 `null` |
| 头条 | 文章 SSR 统计（若首包可解析）、评论总数和一级评论 | `article SSR itemCounter/likeData`、`article/v4/tab_comments` | 评论可用；互动统计受 JSVM/挑战影响 |
| 公众号 | 页面公开字段；有文章会话时读取互动和评论 | 页面 SSR、`getappmsgext`、`appmsg_comment` | 任意文章匿名互动/评论无稳定免费接口；官方统计只适用于自有公众号授权 |
| 快手 | 播放、点赞、评论总数和一级评论 | `visionVideoDetail`、SSR Apollo、REST/GraphQL 评论双通道 | 无需账号；398 条互动和 398 条评论企业实测均有数据，详情已下线时仅返回可验证评论数 |

真实输入 URL 同时兼容头条 `/i{id}`、快手 `c.kuaishou.com/fw/photo/{id}`、微博桌面端
`/用户ID/base62短ID` 和 `live.bilibili.com/{room_id}`。这些变体只在路由层规范化，平台
采集器仍使用同一套目标 ID 校验，不会把用户 ID、推荐内容或其他房间数据当成目标作品。

不要把浏览器抓到的临时 Cookie、`x-s`、`hk_sign` 或其他签名硬编码到服务代码。抖音的
访客状态由运行时向第一方初始化，`a_bogus` 由本地算法按请求即时计算；若协议受风控，
再进入浏览器兜底。B 站评论的 WBI 密钥每次从公开导航接口动态读取，不依赖浏览器或登录
Cookie。

逐项证据和“无法稳定获取”的阻断原因见 [`docs/PROTOCOL_MATRIX.md`](docs/PROTOCOL_MATRIX.md)，
代码入口和执行顺序见 [`docs/PLATFORM_FLOWS.md`](docs/PLATFORM_FLOWS.md)。
本轮自动化、API、CLI、代理和八个平台真实 URL 的验证结果见
[`docs/TEST_REPORT.md`](docs/TEST_REPORT.md)。

头条评论接口已验证：`/article/v4/tab_comments/` 使用 `aid/app_name/offset/count/group_id/item_id` 即可返回 `err_no=0`、`total_number`、`has_more` 和评论列表，不需要把浏览器请求里的 `_signature` 写入代码。

快手不能用“GraphQL 返回 HTTP 200”作为成功判据。项目复用本地游客状态直接调用
`visionVideoDetail` 和 `/rest/v/photo/comment/list`，REST 受限时尝试 GraphQL 评论通道，
详情传输失败时解析目标页 `__APOLLO_STATE__`；所有结果必须严格匹配 `photoId`。游客状态
自然过期后才由浏览器重新生成，不把 `kww/kwssectoken` 写入代码。

小红书评论接口已经定位到 `api/sns/web/v2/comment/page`。未登录网页会建立游客会话并返回首屏评论，但 UI 会阻止继续翻页；项目不会把第一页冒充成第二页。自有登录态可使用 `xhshow==0.2.0` 动态签名继续按游标请求；旧 `XYS_` 返回 406 时会自动重试 `XYW_`，两种格式均失败才标记受阻。
