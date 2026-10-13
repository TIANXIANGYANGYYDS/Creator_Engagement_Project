# API 简明使用文档

更新日期：2026-10-13。接口路径已核对当前运行中的服务。

服务地址：`http://39.106.202.228:8200`

在线调试：[Swagger 接口页面](http://39.106.202.228:8200/docs)。当前调用不需要 API Key；POST 请求使用 `Content-Type: application/json`。

## 支持的平台

当前服务接入 **21 个互动/评论平台、7 个作者作品平台**。请求的 `media_name` 可以填写下表中的中文名称或英文值。

表中的“已接入”表示有对应接口处理逻辑；实际能否取到数据、字段是否齐全和能否继续分页，仍取决于作品可见性、登录状态及平台访问限制。

| 平台名称 | `media_name` 英文值 | 互动/评论接口 | 作者作品接口 |
|---|---|---|---|
| 抖音 | `douyin` | 已接入 | 已接入 |
| 今日头条 | `toutiao` | 已接入 | 已接入 |
| 小红书 | `xiaohongshu` | 已接入 | 已接入 |
| 微博 | `weibo` | 已接入 | 已接入 |
| B站 | `bilibili` | 已接入 | 已接入 |
| 快手 | `kuaishou` | 已接入 | 已接入 |
| 好看视频 | `haokan` | 已接入 | 未接入 |
| 微信公众号 | `wechat` | 已接入 | 未接入 |
| 微信视频号 | `wechat_channels` | 已接入 | 未接入 |
| 新浪看点 | `sina` | 已接入 | 未接入 |
| 百家号 | `baijiahao` | 已接入 | 未接入 |
| 有驾 | `yoojia` | 已接入 | 未接入 |
| 新浪财经头条号 | `sina_finance` | 已接入 | 未接入 |
| 搜狐号 | `sohu` | 已接入 | 未接入 |
| 懂车帝 | `dongchedi` | 已接入 | 未接入 |
| 今日头条微头条 | `weitoutiao` | 已接入 | 使用今日头条作者接口，见下方说明 |
| 网易号 | `netease` | 已接入 | 未接入 |
| 雪球 | `xueqiu` | 已接入 | 未接入 |
| 汽车之家 | `autohome` | 已接入 | 未接入 |
| 西瓜视频 | `ixigua` | 已接入 | 未接入 |
| 腾讯自选股 | `qq_stock` | 已接入 | 未接入 |
| 新浪博客 | `sina_blog` | 未接入 | 已接入 |

微头条作者采集使用 `media_name=今日头条` 或 `toutiao`，属于同一个今日头条作者入口，不单独计为第 8 个作者平台。作者接口也接受中文别名 `今日头条微头条`，但不接受英文 `weitoutiao`。

新浪看点、新浪财经头条号、新浪博客是不同入口；微信公众号和微信视频号也需分别指定。当前运行服务的平台清单可通过 `GET /api/v1/health` 中的 `engagement_platforms`、`author_platforms` 查询。

## 1. 先选接口

| 想获取什么 | 方法 | 路径 |
|---|---|---|
| 一条内容的点赞、评论数等 | GET | `/api/v1/interactions` |
| 一条内容的评论正文 | GET | `/api/v1/comments` |
| 少量链接同步获取 | POST | `/api/v1/collect` |
| 大量链接后台获取 | POST | `/api/v1/jobs` |
| 一个作者的一页作品 | GET | `/api/v1/authors/works` |
| 一个作者的作品后台获取 | POST | `/api/v1/authors/jobs` |
| 多个作者的作品后台获取 | POST | `/api/v1/authors/jobs/batch` |
| 检查服务是否正常 | GET | `/api/v1/health` |

以下响应示例只展示主要字段，数值仅用于说明格式。

## 2. 获取互动数据

`GET /api/v1/interactions`

| 参数 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 作品原始链接 |
| `media_name` | 是 | 平台名称，例如 `小红书`、`抖音` |

```bash
curl --get 'http://39.106.202.228:8200/api/v1/interactions' \
  --data-urlencode 'url=https://www.xiaohongshu.com/discovery/item/6aca066d000000000a0213b8' \
  --data-urlencode 'media_name=小红书'
```

返回示例：

```json
{
  "media_name": "小红书",
  "data": {
    "views": null,
    "likes": 2,
    "comments": 1,
    "shares": null,
    "favorites": null
  }
}
```

| 字段 | 含义 |
|---|---|
| `views` | 阅读量或播放量 |
| `likes` | 点赞数 |
| `comments` | 评论数量，不是评论正文 |
| `shares` | 分享数 |
| `favorites` | 收藏数 |
| `reposts` | 转发数 |
| `coins` / `danmaku` / `recommendations` | 投币数 / 弹幕数 / 推荐数 |

不同平台提供的字段不同。`0` 表示明确取得零值；`null` 表示接口未提供确定数值。已人工核验的小红书“按钮只有文字、没有数字”场景，可以按业务约定视为 0；不能把请求失败或其他未知缺失也统一当成 0。

## 3. 获取评论正文

`GET /api/v1/comments`

参数与互动接口相同，另外可传 `page`（从 1 开始）。**建议先传 `page=1`**；不传时服务会尝试连续翻页，耗时更长，也不保证能取得平台全部历史评论。

```bash
curl --get 'http://39.106.202.228:8200/api/v1/comments' \
  --data-urlencode 'url=https://www.xiaohongshu.com/discovery/item/6aca066d000000000a0213b8' \
  --data-urlencode 'media_name=小红书' \
  --data-urlencode 'page=1'
```

返回示例：

```json
{
  "media_name": "小红书",
  "comments": [
    {"comment_id": "示例评论ID", "author": "示例用户", "text": "评论正文", "likes": 0}
  ]
}
```

`comments: []` 表示本次成功请求没有返回评论。这个简化接口不返回下一页游标或完整性标记；需要判断获取完整性时，使用下面的批量接口查看 `complete`。部分平台的后续页受游标或访问权限限制，不能保证任意页码都能读取。

## 4. 批量获取互动和评论

大量链接推荐使用异步任务：**提交 → 查询状态 → 读取结果**。

### 第一步：提交

`POST /api/v1/jobs`

```json
{
  "items": [
    {
      "item_id": "note-001-interactions",
      "url": "https://www.xiaohongshu.com/discovery/item/6aca066d000000000a0213b8",
      "media_name": "小红书",
      "type": "interactions"
    },
    {
      "item_id": "note-001-comments",
      "url": "https://www.xiaohongshu.com/discovery/item/6aca066d000000000a0213b8",
      "media_name": "小红书",
      "type": "comments",
      "page": 1
    }
  ]
}
```

`item_id` 是调用方设置的标识，在同一个任务内保持唯一。`type` 只能是 `interactions`（互动）或 `comments`（评论）；同一链接要同时取两类数据，就提交两条。

将以上 JSON 保存为 `request.json` 后调用：

```bash
curl -X POST 'http://39.106.202.228:8200/api/v1/jobs' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

返回 HTTP 202：

```json
{"job_id": "服务返回的任务ID", "status": "queued"}
```

### 第二步：查询状态

`GET /api/v1/jobs/{job_id}`，把 `{job_id}` 替换成返回值。建议每 5～10 秒查询一次。

```json
{
  "job_id": "服务返回的任务ID",
  "status": "completed",
  "progress": {"total": 2, "completed": 2, "success": 1, "failed": 1}
}
```

`queued`：排队；`running`：运行；`completed`：处理结束；`failed`：任务失败；`cancelled`：已取消。**处理结束不代表每条都成功，要看 `progress.failed` 和逐条结果。**

### 第三步：读取结果

`GET /api/v1/jobs/{job_id}/results?limit=100`

结果在 `data` 数组中。每条包含 `item_id`、`status`、`complete`、`result`、`error`。

- `status=success`：本条取得可用数据；`failed`：查看 `error`。
- 互动结果中的评论数叫 `result.total_comments`；评论正文叫 `result.comments`。
- `complete` 是本次请求范围的完整性标记：指定 `page=1` 时只对应这一页；互动为 `true` 也不代表所有统计字段都有值。不指定评论页码时，`false` 表示不能确认评论全集完整。
- 有 `next_cursor` 时，将原值作为下一次请求的 `cursor` 参数；`limit` 范围 1～500。
- 运行中也能读已完成结果；当前无下一页不代表任务结束，还要结合任务状态继续查询。结束后及时保存结果，默认保留 24 小时。

取消：`POST /api/v1/jobs/{job_id}/cancel`。续跑：`POST /api/v1/jobs/{job_id}/resume`。这两个请求不需要 JSON 请求体。

少量链接也可以同步调用 `POST /api/v1/collect`，请求格式同上但不需要 `item_id`，响应直接返回 `data` 数组。同步请求总时限为 120 秒，耗时较长的采集请使用任务接口。

## 5. 获取作者作品

作者接口支持：**抖音、快手、微博、B站、小红书、今日头条、新浪博客**。具体可获取范围仍受平台和会话限制。

单页请求：

```bash
curl --get 'http://39.106.202.228:8200/api/v1/authors/works' \
  --data-urlencode 'author_id=3402362465301307' \
  --data-urlencode 'media_name=今日头条'
```

两个参数均必填：`author_id` 是账号标识，不是昵称或作品 URL；`media_name` 是平台名称。返回的 `data` 是作品列表，含 `work_id`、`url`、`title`、`text`、`published_at` 等；`next_cursor` 非空时，保持相同作者和平台，将其原值作为 `cursor` 继续请求。

连续采集历史作品推荐提交后台任务：

```bash
curl -X POST 'http://39.106.202.228:8200/api/v1/authors/jobs' \
  -H 'Content-Type: application/json' \
  -d '{"author_id":"3402362465301307","media_name":"今日头条"}'
```

拿到 `job_id` 后：

| 操作 | 方法与路径 |
|---|---|
| 查询进度 | `GET /api/v1/authors/jobs/{job_id}` |
| 读取作品 | `GET /api/v1/authors/jobs/{job_id}/results?limit=100` |
| 取消 / 续跑 | `POST /api/v1/authors/jobs/{job_id}/cancel` / `resume` |

状态中 `saved_count` 是已发现作品数，`detail_completed_count` 是详情完成数，`detail_failed_count` 是详情失败数。`partial` 表示只完成部分获取；只有 `complete=true` 才表示满足该任务的完整性条件。

批量作者使用 `POST /api/v1/authors/jobs/batch`，请求体直接传数组：

```json
[
  {"author_id": "3402362465301307", "media_name": "今日头条"},
  {"author_id": "7946463148", "media_name": "微博"}
]
```

响应 `data` 逐条给出 `job_id` 和提交状态：`accepted`（已接收）、`duplicate`（同批重复）、`rejected`（参数不合格）。作者 ID 格式和详细分页规则见 [作者接口文档](AUTHOR_CONTENT_API.md)。

## 6. 平台名称

完整平台清单与各接口支持范围见文档开头“支持的平台”表。`media_name` 例如填写 `抖音` 或 `douyin`、`小红书` 或 `xiaohongshu`、`B站` 或 `bilibili`，不要填写作者昵称或作品标题。

作品 URL 必须与 `media_name` 对应；作者接口只支持第 5 节列出的平台。

## 7. 请求失败时怎么办

| HTTP 状态 | 含义与处理 |
|---|---|
| `200` | 请求完成；批量请求还需检查每条 `status` |
| `202` | 后台任务已接收，继续查进度 |
| `422` | 参数不正确，检查 URL、平台名、页码或作者 ID |
| `404` | 查询的任务不存在或已过期 |
| `409` | 当前任务不能续跑，或重复提交标识冲突；查看 `detail` |
| `502` | 目标数据不可用或上游请求失败；查看 `detail`，不直接判定账号掉线 |
| `504` | 本次请求超时；大量采集改用异步任务 |
| `500` | 服务内部异常，保留响应头 `X-Request-ID` 便于排查 |

单项错误通常返回 `{"detail":"错误原因"}`；参数校验错误的 `detail` 也可能是列表。批量条目失败看 `data` 中对应项的 `error`。

URL 含 `&`、`?` 时使用示例中的 `--data-urlencode`，或通过客户端的 `params` 参数传入，保留原始分享链接上的访问参数。小红书互动/评论由服务端使用已登录的本机直连会话并统一限速，调用方不需要传 Cookie；批量采集优先提交任务，避免超时后连续重复提交。

更多字段见 [完整 API 文档](API.md)，运行状态告警见 [告警说明](STATUS_ALERTS.md)。
