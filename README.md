# Creator Engagement Project

根据公开内容 URL 获取互动量和一级评论的独立项目，与 `Stock_Project` 无代码依赖。当前部署
运行于 `MyAgent`（Python 3.13.12），采用协议优先、浏览器兜底、代理池复用和企业级重试。

项目当前支持九个渠道：抖音、今日头条、微信公众号、微信视频号、小红书、好看视频、快手、
B 站和微博。微信公众号与微信视频号是两个不同的平台适配器。

## 当前业务边界

默认配置为 `STRICT_ANONYMOUS_MODE=true`，生产流程遵守以下约束：

- 不需要人工登录、扫码或真实平台账号。
- 不接入付费数据供应商。
- 历史账号 Cookie、公众号凭据、微信侧车和账号 Profile 均被忽略。
- 只获取一级评论正文，不获取评论下的回复正文。
- 评论对象中的 `replies` 仅表示平台返回的回复数量，不会触发额外请求。
- “全部评论”指匿名访客可见的全部一级评论，不包含删除、隐藏、审核折叠或仅账号可见内容。

当前一级评论能力：

| 平台 | 互动量 | 一级评论正文 | 一级评论分页 |
|---|---|---|---|
| 抖音 | 匿名协议可用 | 可用 | 可翻到公开末游标 |
| 今日头条 | 可用，部分文章受 SSR 挑战影响 | 可用 | 可按 offset 翻到公开末页 |
| 微信公众号 | 严格匿名模式不可稳定获取 | 不可用 | 不适用 |
| 微信视频号 | 匿名公开预览可用，不含播放量 | 不可用 | 不适用 |
| 小红书 | 带有效 `xsec_token` 的 URL 可匿名获取 | 不可用 | 不适用 |
| 好看视频 | 可用，未公开字段保持 `null` | 可用 | 可按页翻到公开末页 |
| 快手 | 自动游客状态可用 | 可用 | 可按游标翻到公开末页 |
| B 站 | 视频、专栏和 Opus 可用 | 可用 | 可翻到公开末页 |
| 微博 | 访客公开字段可用 | 可用 | 可翻多页，但不能保证到底 |

当前没有固定“只能获取第一页”的平台。微博已经验证能够获取第二页，但更深页可能触发
`ok=-100`、登录跳转或风控，因此不能归为“仅第一页”，也不能承诺全部。

B 站页面的评论总数可能包含评论下的回复数量，所以一级评论行数少于 `total_comments` 不一定
表示漏页。B 站直播不在业务范围内，`live.bilibili.com` 会在请求上游前被拒绝。

## 对外接口

服务只保留两个数据接口：

```text
GET /api/v1/interactions?url=<URL>&media_name=<MEDIA>
GET /api/v1/comments?url=<URL>&media_name=<MEDIA>&page=1
```

另有健康检查：

```text
GET /api/v1/health
```

互动量与评论是两个独立业务请求，分别执行并分别缓存。一个代理 IP 可以在有效期内承载多个
HTTP 请求；复用同一个 IP 不等于把互动量和评论合并成一次上游请求。

`media_name` 的规范值为：

```text
douyin / toutiao / wechat / wechat_channels / xiaohongshu /
haokan / kuaishou / bilibili / weibo
```

接口也接受抖音、头条、公众号、微信视频号、小红书、好看、快手、B站和微博等中文名称。
服务会同时校验 URL 平台和 `media_name`；两者不一致时返回 HTTP 422。

### 调用示例

互动量：

```bash
curl --get 'http://127.0.0.1:8200/api/v1/interactions' \
  --data-urlencode 'url=<内容 URL>' \
  --data-urlencode 'media_name=抖音'
```

一级评论第 1 页：

```bash
curl --get 'http://127.0.0.1:8200/api/v1/comments' \
  --data-urlencode 'url=<内容 URL>' \
  --data-urlencode 'media_name=抖音' \
  --data-urlencode 'page=1'
```

评论页固定最多返回 20 条。响应中的 `next_page` 不为空时，继续请求对应页码。

### 响应语义

互动量响应主要字段：

```text
platform / canonical_url / work_id / stats / coverage / reason / source
```

一级评论响应主要字段：

```text
platform / canonical_url / work_id / page / comments / next_page /
total_comments / capabilities / coverage / reason / source
```

`capabilities.root_comments` 的可能值：

- `all_public_pages`：可以按当前匿名协议翻到公开结束位置。
- `paged_until_blocked`：可以翻多页，但深页可能被平台拦截。
- `first_public_page`：只能稳定获取公开第一页；当前没有平台使用该状态。
- `unavailable`：严格匿名模式下不能获取评论正文。

`coverage` 的可能值：

- `complete`：当前响应可以完整说明本页或明确的空结果。
- `partial`：返回了可验证数据，但字段、可见范围或平台口径不完整。
- `blocked`：验证码、登录挑战、频控或其他平台拒绝。
- `failed`：网络、协议或解析失败。
- `unsupported`：当前约束下没有可验证的数据来源。

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

也可以直接使用 CLI：

```bash
conda run -n MyAgent creator-engagement interactions '<内容 URL>' bilibili
conda run -n MyAgent creator-engagement comments '<内容 URL>' B站 --page 1
```

增加 `--direct` 可以让单次 CLI 调用绕过代理配置。

## 配置、代理和稳定性

运行配置读取自 `.local/env/.env`，完整模板见 `.env.example`。当前主要配置：

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `STRICT_ANONYMOUS_MODE` | `true` | 禁止复用账号 Cookie、凭据、侧车和历史账号 Profile |
| `PROXY_MODE` | `prefer` | 有代理配置时优先代理，无代理时允许直连 |
| `PROXY_51_API_URL` | 空 | Stock 项目同源的 51 代理供应接口 |
| `PROXY_POOL_SIZE` | `4` | 代理池目标数量 |
| `PROXY_MAX_CONCURRENCY` | `1` | 单个代理的最大并发 |
| `RELIABILITY_MODE` | `enterprise` | 启用协议重试、语义失败换 IP 和平台保护 |
| `PROTOCOL_MAX_ATTEMPTS` | `3` | 企业模式最大协议尝试次数 |
| `COLLECTION_MAX_CONCURRENCY` | `4` | 全局采集并发 |
| `BROWSER_FALLBACK_ENABLED` | `true` | 协议不可用时允许浏览器兜底 |
| `BROWSER_MAX_CONCURRENCY` | `1` | 浏览器最大并发，避免服务器内存被打满 |
| `ENGAGEMENT_CACHE_TTL_SECONDS` | `120` | 成功结果缓存时间 |
| `ENGAGEMENT_CACHE_MAX_ENTRIES` | `1000` | 最大缓存项数 |

代理池支持批量补池、IP TTL、单 IP 并发限制、失败淘汰和供应商 API 限流。一次业务调用内部的
预热和数据请求可以共用代理租约，但每个 HTTP 调用仍分别计入上游请求量。

默认企业模式最多进行 3 次协议尝试。HTTP 200 空包、验证码或缺少目标字段不会被当作成功；
可重试的语义失败会淘汰当前代理。相同接口、相同 URL、相同页码的并发请求会合并，成功或
有效部分结果缓存 120 秒。

浏览器兜底默认限制为单并发。严格匿名模式下，浏览器和游客状态写入：

```text
.local/browser-profiles/anonymous/<platform>
.local/platform-sessions/anonymous/<platform>.json
```

公众号、微信视频号和小红书评论不会通过历史账号 Profile 或浏览器登录绕过严格匿名边界。
抖音签名、B 站 WBI 密钥以及快手短期游客状态均在运行时生成，不应硬编码到源码或日志。

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
173 passed
ruff check app tests: All checks passed
```

本地复验：

```bash
conda run -n MyAgent pytest -q
conda run -n MyAgent ruff check app tests
conda run -n MyAgent python -m compileall -q app tests
```

## 详细文档

- [九个平台独立执行流程](docs/PLATFORM_FLOWS.md)
- [协议能力矩阵](docs/PROTOCOL_MATRIX.md)
- [浏览器兜底边界](docs/BROWSER_FALLBACK.md)
- [成本、容量与服务器保护](docs/COST_AND_CAPACITY.md)
- [HTML 成本单](docs/ENGAGEMENT_COST_SHEET.html)
- [真实测试报告](docs/TEST_REPORT.md)

历史账号、公众号 Cookie、官方自有号和微信客户端侧车相关文档仅用于兼容代码维护；默认严格
匿名部署不会启用这些能力，也不应把它们计入当前生产成功率。
