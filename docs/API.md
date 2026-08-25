# Creator Engagement API 接口文档

最后更新：2026-08-26
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
| `GET` | `/api/v1/interactions` | 获取内容的互动统计 |
| `GET` | `/api/v1/comments` | 获取内容的一级评论正文 |
| `GET` | `/api/v1/health` | 获取服务和代理池运行状态 |

当前接口不要求业务鉴权 Header，所有业务参数均通过 URL Query 传递，没有 JSON 请求体。两个
业务接口成功时统一返回最小 `{"data": ...}` 结构。生产部署仍应在网关层配置 HTTPS、访问
控制、限流和请求日志脱敏。

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
| `bilibili` | `B站` |
| `weibo` | `微博` |

服务会同时识别 URL 所属平台并校验 `media_name`。二者不一致时返回 HTTP 422。微信公众号和
微信视频号是两个独立平台；公众号使用 `wechat`，视频号使用 `wechat_channels`。

## 3. 获取互动统计

### 3.1 请求

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
  --data-urlencode 'media_name=B站'
```

### 3.2 成功响应

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
| `coins` | integer/null | B站投币量 |
| `danmaku` | integer/null | B站弹幕量 |
| `reposts` | integer/null | 转发量，主要用于微博或B站文章 |
| `recommendations` | integer/null | 推荐量 |

平台没有公开某项数据时返回 `null`，不会使用 `0` 代替未知数据。真实的 `0` 仍返回数字 `0`。
部分平台可能增加扩展字段，当前包括抖音的 `admire/recommend` 和头条的 `show_count`；下游应
读取所需字段并忽略未知字段。

各平台当前主要互动字段：

| 平台 | 当前可返回字段 |
|---|---|
| 抖音 | 播放、点赞、评论数、分享、收藏，及页面存在的赞赏/推荐 |
| 今日头条 | 阅读、点赞、评论数、分享、展示量 |
| 微信公众号 | 严格匿名部署下不能稳定保证，第一页无可验证数据时返回 502 |
| 微信视频号 | 点赞、评论数、分享、收藏；无精确播放量 |
| 小红书 | 点赞、收藏、评论数、分享；URL 需要有效 `xsec_token` |
| 好看视频 | 播放、点赞、评论数 |
| 快手 | 播放、点赞、评论数 |
| B站 | 视频支持播放、点赞、评论、分享、收藏、投币、弹幕；专栏/Opus 按页面公开字段返回 |
| 微博 | 点赞、评论数、转发数 |

## 4. 获取一级评论

### 4.1 请求

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

### 4.2 分页语义

- 不传 `page`：从第 1 页顺序获取到当前可见末页，按 `comment_id` 去重后一次返回。
- 传 `page=N`：只返回第 N 页，每页最多 20 条，不包含前面页面。
- 默认全量遇到深页登录限制或风控：返回此前已经验证的评论数据。
- 第一页即不可用、平台不支持评论正文或没有可验证数据来源：返回 HTTP 502。
- 默认全量具有异常游标和 10000 页安全保护，防止错误上游响应造成无限循环。

“全部”表示当前匿名访客或已配置小红书 Cookie 会话可见的全部一级评论，不包括平台已删除、
隐藏、审核折叠、仅作者可见或当前会话不可见的内容。微博深页可能被平台阻断，因此只能返回
阻断前已获取的数据。

默认全量请求的耗时和上游请求数会随评论页数增加。下游有固定延迟要求时应显式传入 `page`。

### 4.3 成功响应

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

接口不请求或返回二级评论正文。B站页面展示的评论总数可能包含二级回复，因此平台总数可能
大于最终返回的一级评论行数。

## 5. 健康检查

### 5.1 请求

```http
GET /api/v1/health
```

### 5.2 响应

```json
{
  "status": "ok",
  "proxy_mode": "prefer",
  "proxy_configured": true,
  "reliability_mode": "enterprise",
  "protocol_max_attempts": 3,
  "proxy_pool_size": 4,
  "proxy_max_concurrency": 1
}
```

健康检查不是业务数据接口，因此不使用 `data` 外壳。

## 6. 错误响应

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

## 7. Python 调用示例

```python
import requests


base_url = "http://39.106.202.228:8200"
content_url = "https://www.bilibili.com/video/BVxxxxxxxxxx"

interaction_response = requests.get(
    f"{base_url}/api/v1/interactions",
    params={"url": content_url, "media_name": "B站"},
    timeout=60,
)
interaction_response.raise_for_status()
stats = interaction_response.json()["data"]

comment_response = requests.get(
    f"{base_url}/api/v1/comments",
    params={"url": content_url, "media_name": "B站"},
    timeout=300,
)
comment_response.raise_for_status()
comments = comment_response.json()["data"]
```

互动量和评论是两个独立业务请求，分别执行并缓存。默认全量评论可能需要多次平台上游请求，
因此调用方应为它配置比互动量接口更长的超时时间。
