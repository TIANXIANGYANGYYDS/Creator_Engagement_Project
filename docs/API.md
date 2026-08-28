# Creator Engagement API 接口文档

最后更新：2026-08-27
接口版本：`v1`

## 1. 接口概览

服务端监听地址：

```text
0.0.0.0:8200
```

调用方必须使用服务端可达 IP 或域名，不能请求 `0.0.0.0` 或服务端自己的 `127.0.0.1`。当前
服务器的公网调用地址为：

```text
http://39.106.202.228:8200
```

同一内网也可以使用 `http://10.0.0.45:8200`。TCP 8200 已实测可从公网访问；当前接口没有
业务鉴权，生产环境建议在云安全组中只允许下游服务器的来源 IP。

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/collect` | 批量获取多个平台的互动量或评论 |
| `GET` | `/api/v1/interactions` | 获取内容的互动统计 |
| `GET` | `/api/v1/comments` | 获取内容的一级评论正文 |
| `GET` | `/api/v1/health` | 获取服务和代理池运行状态 |

当前接口不要求业务鉴权 Header。批量接口使用 JSON 请求体；两个单项接口继续使用 URL Query。
生产部署仍应在网关层配置 HTTPS、访问控制、限流和请求日志脱敏。

服务启动后可以查看自动生成的接口定义：

```text
Swagger UI:  http://39.106.202.228:8200/docs
ReDoc:       http://39.106.202.228:8200/redoc
OpenAPI:     http://39.106.202.228:8200/openapi.json
```

## 2. 媒体名称

`media_name` 支持规范英文名称和下列中文别名：

| 规范值 | 可用别名 |
|---|---|
| `douyin` | `抖音` |
| `toutiao` | `头条`、`今日头条` |
| `wechat` | `weixin`、`微信`、`公众号`、`微信公众号` |
| `wechat_channels` | `wechat-channels`、`channels`、`视频号`、`微信视频号` |
| `xiaohongshu` | `xhs`、`小红书` |
| `haokan` | `好看`、`好看视频` |
| `kuaishou` | `快手` |
| `bilibili` | `哔哩哔哩`、`B站` |
| `weibo` | `微博` |

服务会同时识别 URL 所属平台并校验 `media_name`。二者不一致时，单项 GET 接口返回 HTTP
422，批量接口将对应项标记为 `failed` 并继续处理其他项。微信公众号和微信视频号是两个
独立平台；批量响应中的“微信”指微信公众号，公众号使用 `wechat`，视频号使用
`wechat_channels`。

批量接口的请求建议使用规范中文名，响应一律返回规范中文名：`抖音`、`今日头条`、`微信`、
`微信视频号`、`小红书`、`好看视频`、`快手`、`哔哩哔哩`、`微博`。输入仍接受
`微信公众号` 和 `B站` 等兼容别名。

微信视频号支持以下 URL：

- `https://weixin.qq.com/sph/...`
- `https://channels.weixin.qq.com/finder-preview/pages/sph?id=...`
- `https://channels.weixin.qq.com/finder-preview/pages/feed?eid=...&token=...`
- `https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?...extInfo.feedID=export/...`

前三类公开分享 URL 可匿名读取页面公开互动量。最后一类客户端跳转 URL 不公开视频数据，互动量
和评论正文都必须使用授权 Windows 微信侧车。未配置或无法连接侧车时，单项接口返回 HTTP 502，
批量项为 `failed`；服务不会查询其他项目或缓存型数据源，也不会把空值伪装为成功。

## 3. 批量采集

### 3.1 请求

```http
POST /api/v1/collect
Content-Type: application/json
```

```json
{
  "items": [
    {
      "url": "https://www.douyin.com/video/1234567890",
      "media_name": "抖音",
      "type": "interactions"
    },
    {
      "url": "https://www.kuaishou.com/short-video/abcdef",
      "media_name": "快手",
      "type": "comments"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | array | 是 | 采集项列表，至少一项，不设置业务条数上限 |
| `items[].url` | string | 是 | 受支持的内容 URL |
| `items[].media_name` | string | 是 | 中文媒体名称；服务同时校验 URL 平台 |
| `items[].type` | string | 是 | `interactions` 或 `comments` |
| `items[].page` | integer/null | 否 | 不传或传 `null` 时获取全部当前可见一级评论；传数字时只获取对应页，必须大于等于 1 |

`page` 必须是 JSON 数字，字符串形式的 `"1"` 不作为页码接受。

批量接口没有固定条数上限，但服务仍按全局并发、平台并发和平台请求间隔排队执行。批次越大，
响应时间越长，并仍受调用方、网关和服务器的请求体大小及 HTTP 超时约束。

### 3.2 响应

```json
{
  "data": [
    {
      "url": "https://www.douyin.com/video/1234567890",
      "media_name": "抖音",
      "type": "interactions",
      "status": "partial",
      "result": {
        "views": 1000,
        "likes": 80,
        "total_comments": 12,
        "shares": 5,
        "favorites": 3,
        "coins": null,
        "danmaku": null,
        "reposts": null,
        "recommendations": null,
        "comment_list": null
      },
      "error": null
    }
  ],
  "duration_ms": 1830,
  "cost_yuan": 0.00084
}
```

每一项始终包含相同的 `result` 字段。平台未公开的互动字段返回 `null`；未请求评论时
`comment_list` 为 `null`；评论请求成功且没有可见评论时为 `[]`。`status` 的取值为：

- `success`：平台明确返回完整的当前请求结果；
- `partial`：返回了可验证数据，但平台公开能力不足以证明完整覆盖；
- `failed`：该项没有可用数据，固定结果字段均为 `null`，原因写入 `error`。

`duration_ms` 是整个批次的端到端耗时。`cost_yuan` 当前仅统计本批次触发代理池新增 IP 的
采购成本，公式为 `新增 IP 数 × 0.00084 元`；复用已有代理、直连和缓存命中不会产生新增
代理成本。它不是对客户的商业收费金额。

统一互动字段的当前平台覆盖如下；实际页面没有公开某个值时仍返回 `null`：

| 平台 | views | likes | total_comments | shares | favorites | coins | danmaku | reposts | recommendations | comment_list |
|---|---|---|---|---|---|---|---|---|---|---|
| 抖音 | 支持 | 支持 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 部分作品 | 支持 |
| 今日头条 | 支持 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 |
| 微信 | 取决于授权会话 | 取决于授权会话 | 取决于授权会话 | 取决于授权会话 | 自有号可用 | 不支持 | 不支持 | 不支持 | 自有号可用 | 严格匿名不可用 |
| 微信视频号 | 公开页或授权侧车可用 | 支持 | 支持 | 支持 | 公开页支持 | 不支持 | 不支持 | 不支持 | 不支持 | 授权侧车可用 |
| 小红书 | 不支持 | 支持 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 匿名首批可用；Cookie 模式可深分页 |
| 好看视频 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 |
| 快手 | 支持 | 支持 | 支持 | 当前未公开 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 |
| 哔哩哔哩 | 视频/专栏可用 | 支持 | 支持 | 支持 | 支持 | 支持 | 视频可用 | 专栏可用 | 不支持 | 支持 |
| 微博 | 不支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 | 不支持 | 支持但深页可能受限 |

## 4. 获取互动统计

### 4.1 请求

```http
GET /api/v1/interactions?url=<URL>&media_name=<MEDIA>
```

| 参数 | 类型 | 必填 | 规则 | 说明 |
|---|---|---:|---|---|
| `url` | string | 是 | 非空、受支持的内容 URL | 文章、视频或作品页面地址 |
| `media_name` | string | 是 | 参见媒体名称表 | 用于选择对应平台采集器 |

示例：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/interactions' \
  --data-urlencode 'url=https://www.bilibili.com/video/BVxxxxxxxxxx' \
  --data-urlencode 'media_name=哔哩哔哩'
```

### 4.2 成功响应

HTTP 200：

```json
{
  "data": {
    "views": 100000,
    "likes": 5200,
    "comments": 830,
    "shares": 210,
    "favorites": 460,
    "coins": 120,
    "danmaku": 320,
    "reposts": null,
    "recommendations": null
  }
}
```

`data` 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `views` | integer/null | 播放量或阅读量 |
| `likes` | integer/null | 点赞量 |
| `comments` | integer/null | 评论总数，不是评论正文 |
| `shares` | integer/null | 分享量 |
| `favorites` | integer/null | 收藏量 |
| `coins` | integer/null | 哔哩哔哩投币量 |
| `danmaku` | integer/null | 哔哩哔哩弹幕量 |
| `reposts` | integer/null | 转发量，主要用于微博或哔哩哔哩文章 |
| `recommendations` | integer/null | 推荐量 |

平台没有公开某项数据时返回 `null`，不会使用 `0` 代替未知数据。真实的 `0` 仍返回数字 `0`。
部分平台可能增加扩展字段，当前包括抖音的 `admire/recommend` 和头条的 `show_count`；下游应
读取所需字段并忽略未知字段。

各平台当前主要互动字段：

| 平台 | 当前可返回字段 |
|---|---|
| 抖音 | 播放、点赞、评论数、分享、收藏，及页面存在的赞赏/推荐 |
| 今日头条 | 阅读、点赞、评论数、分享、展示量 |
| 微信 | 指微信公众号；严格匿名部署下不能稳定保证，第一页无可验证数据时返回 502 |
| 微信视频号 | 公开分享页返回点赞、评论数、分享、收藏；客户端 `feedID` 仅由授权侧车返回平台会话可见互动量 |
| 小红书 | 点赞、收藏、评论数、分享；URL 需要有效 `xsec_token` |
| 好看视频 | 播放、点赞、评论数 |
| 快手 | 播放、点赞、评论数 |
| 哔哩哔哩 | 视频支持播放、点赞、评论、分享、收藏、投币、弹幕；专栏/Opus 按页面公开字段返回 |
| 微博 | 点赞、评论数、转发数 |

## 5. 获取一级评论

### 5.1 请求

默认获取全部当前可见一级评论：

```http
GET /api/v1/comments?url=<URL>&media_name=<MEDIA>
```

只获取指定页：

```http
GET /api/v1/comments?url=<URL>&media_name=<MEDIA>&page=<N>
```

| 参数 | 类型 | 必填 | 规则 | 说明 |
|---|---|---:|---|---|
| `url` | string | 是 | 非空、受支持的内容 URL | 文章、视频或作品页面地址 |
| `media_name` | string | 是 | 参见媒体名称表 | 用于选择对应平台采集器 |
| `page` | integer | 否 | `>= 1` | 不传时获取全部；传入时只返回第 N 页 |

默认全量示例：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/comments' \
  --data-urlencode 'url=https://www.douyin.com/video/1234567890' \
  --data-urlencode 'media_name=抖音'
```

指定第 3 页示例：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/comments' \
  --data-urlencode 'url=https://www.douyin.com/video/1234567890' \
  --data-urlencode 'media_name=抖音' \
  --data-urlencode 'page=3'
```

### 5.2 分页语义

- 不传 `page`：从第 1 页顺序获取到当前可见末页，按 `comment_id` 去重后一次返回。
- 传 `page=N`：只返回第 N 页，每页最多 20 条，不包含前面页面。
- 默认全量遇到深页登录限制或风控：返回此前已经验证的评论数据。
- 小红书匿名模式通过本地游客浏览器读取页面公开的首批评论；页面出现登录门槛后停止。配置
  有效小红书 Cookie 后才按 cursor 继续深分页。
- 第一页即不可用、平台不支持评论正文或没有可验证数据来源：返回 HTTP 502。
- 默认全量具有异常游标和 10000 页安全保护，防止错误上游响应造成无限循环。

“全部”表示当前会话可顺序访问的全部一级评论，不包括平台已删除、隐藏、审核折叠、仅作者
可见或当前会话不可见的内容。小红书匿名游客通常只能访问首批；微博深页可能被平台阻断，
因此只能返回阻断前已获取的数据。

默认全量请求的耗时和上游请求数会随评论页数增加。下游有固定延迟要求时应显式传入 `page`。

### 5.3 成功响应

HTTP 200：

```json
{
  "data": [
    {
      "comment_id": "739000000000001",
      "author": "用户昵称",
      "text": "这是一条一级评论",
      "created_at": "2026-08-25T03:20:31Z",
      "likes": 26,
      "replies": 3
    }
  ]
}
```

没有一级评论时：

```json
{
  "data": []
}
```

评论字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `comment_id` | string | 平台一级评论 ID |
| `author` | string | 评论者昵称；平台没有公开时为空字符串 |
| `text` | string | 一级评论正文 |
| `created_at` | string/null | ISO 8601 评论时间；无法验证时为 `null` |
| `likes` | integer/null | 该评论的点赞量 |
| `replies` | integer/null | 该评论下的回复数量，不包含回复正文 |

接口不请求或返回二级评论正文。哔哩哔哩页面展示的评论总数可能包含二级回复，因此平台总数可能
大于最终返回的一级评论行数。

## 6. 健康检查

### 6.1 请求

```http
GET /api/v1/health
```

### 6.2 响应

```json
{
  "status": "ok",
  "proxy_mode": "prefer",
  "proxy_configured": true,
  "reliability_mode": "enterprise",
  "protocol_max_attempts": 3,
  "proxy_pool_size": 8,
  "proxy_max_concurrency": 1,
  "collection_max_concurrency": 8,
  "toutiao_protocol_max_attempts": 1,
  "douyin_protocol_max_attempts": 5,
  "browser_max_concurrency": 3,
  "browser_max_attempts": 3,
  "browser_geoip_enabled": false
}
```

健康检查不是业务数据接口，因此不使用 `data` 外壳。

## 7. 错误响应

批量请求的 JSON 结构、`type` 或 `page` 不合法时返回 HTTP 422；请求体合法后，单项 URL、
媒体或采集失败写入该项的 `status/error`，不会让其他项失败。

| HTTP 状态 | 场景 |
|---:|---|
| `422` | 缺少参数、`page < 1`、不支持的 URL、媒体名称不支持或媒体与 URL 不匹配 |
| `502` | 平台拦截、验证码、协议失败、互动字段全部不可用或评论第一页不可用 |
| `500` | 未预期的服务端错误 |

业务校验或采集失败通常返回：

```json
{
  "detail": "media_name '微博' does not match URL platform 'douyin'"
}
```

FastAPI 参数格式校验的 `detail` 是错误数组，例如 `page=0`：

```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": ["query", "page"],
      "msg": "Input should be greater than or equal to 1",
      "input": "0",
      "ctx": {"ge": 1}
    }
  ]
}
```

未预期错误不会暴露内部异常：

```json
{
  "detail": "服务器内部错误"
}
```

下游应先判断 HTTP 状态码，只有 HTTP 200 时读取 `data`。`data: []` 表示成功确认当前请求
没有返回一级评论；它与 HTTP 502 的“无法获取评论”含义不同。

## 8. Python 调用示例

```python
import requests


base_url = "http://39.106.202.228:8200"
content_url = "https://www.bilibili.com/video/BVxxxxxxxxxx"

interaction_response = requests.get(
    f"{base_url}/api/v1/interactions",
    params={"url": content_url, "media_name": "哔哩哔哩"},
    timeout=60,
)
interaction_response.raise_for_status()
stats = interaction_response.json()["data"]

comment_response = requests.get(
    f"{base_url}/api/v1/comments",
    params={"url": content_url, "media_name": "哔哩哔哩"},
    timeout=300,
)
comment_response.raise_for_status()
comments = comment_response.json()["data"]
```

互动量和评论是两个独立业务请求，分别执行并缓存。默认全量评论可能需要多次平台上游请求，
因此调用方应为它配置比互动量接口更长的超时时间。
