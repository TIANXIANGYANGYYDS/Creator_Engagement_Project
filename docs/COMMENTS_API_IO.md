# 评论接口 I/O

更新日期：2026-10-31。服务地址：`http://39.106.202.228:8200`。

各来源的正文/分页限制见 [平台清单](PLATFORM_SUPPORT.md)，不能把注册来源数量当作完整评论能力数量。

## 1. 单链接评论

**请求：`GET /api/v1/comments`**

参数放在 URL query 中，无请求体。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | string | 是 | 作品原始 URL；通过 query 传递时需 URL 编码 |
| `media_name` | string | 是 | 平台名称，例如 `小红书`、`今日头条`、`搜狐号`、`雪球`、`股吧` |
| `page` | integer | 否 | 从 1 开始；传入时只取指定页；不传则从第一页连续获取当前可访问的评论并去重 |

调用方不需要额外提交作者信息、Cookie 或 `xsec_token`。小红书由服务端使用已有真实账号、本机 IP 和独立限频尝试恢复访问上下文；不保证任意链接都能采集成功。

```bash
curl --get 'http://39.106.202.228:8200/api/v1/comments' \
  --data-urlencode 'url=https://www.sohu.com/a/1094975590_430526' \
  --data-urlencode 'media_name=搜狐号' \
  --data-urlencode 'page=1'
```

**成功输出：HTTP 200，`application/json`。** 以下数值和正文仅用于说明结构，不是上述链接的完整实测结果。

```json
{
  "media_name": "搜狐号",
  "comments": [
    {
      "comment_id": "1234567890123456789",
      "author": "示例用户",
      "text": "这是一条一级评论",
      "created_at": "2026-10-30T19:00:00+08:00",
      "likes": 2,
      "replies": 1
    }
  ]
}
```

| 输出字段 | 类型 | 说明 |
|---|---|---|
| `media_name` | string | 规范化后的中文平台名称 |
| `comments` | array | 本次返回的一级评论列表；没有返回评论时为 `[]` |
| `comments[].comment_id` | string | 评论 ID，按字符串保存，避免长整数精度丢失 |
| `comments[].author` | string | 评论者昵称；未知时为空字符串 |
| `comments[].text` | string | 一级评论正文 |
| `comments[].created_at` | string / null | ISO 8601 时间，带时区；未知时为 `null` |
| `comments[].likes` | integer / null | 该条评论的点赞数；未知时为 `null` |
| `comments[].replies` | integer / null | 该条评论下的回复数；不返回回复正文，未知时为 `null` |

分页与完整性：

- 建议先用 `page=1` 对接；页大小由平台决定，没有 `page_size` 参数。
- 当前 GET 响应只有 `media_name` 和 `comments`，没有评论总数、下一页游标或完整性标记。
- 不传 `page` 时，后续页受限可能返回已经取得的评论；HTTP 200 不代表获取了全部历史评论。
- `0` 表示明确零值，`null` 表示未知；空列表不等于采集失败，也不能据此断言整篇作品没有评论。

## 2. 需要评论总数、逐项状态时

**请求：`POST /api/v1/collect`，`Content-Type: application/json`。**

```json
{
  "items": [
    {
      "url": "https://www.sohu.com/a/1094975590_430526",
      "media_name": "搜狐号",
      "type": "comments",
      "page": 1
    }
  ]
}
```

`items` 是非空数组；每项 `url`、`media_name`、`type` 必填，评论操作固定为 `comments`。
`page` 可省略；传入时必须是大于等于 1 的 JSON 整数，不能是字符串。省略时连续翻页。

成功输出结构示例：

```json
{
  "data": [
    {
      "url": "https://www.sohu.com/a/1094975590_430526",
      "media_name": "搜狐号",
      "type": "comments",
      "status": "success",
      "complete": true,
      "result": {
        "views": null,
        "likes": null,
        "total_comments": 2,
        "shares": null,
        "favorites": null,
        "coins": null,
        "danmaku": null,
        "reposts": null,
        "recommendations": null,
        "comments": [
          {
            "comment_id": "1234567890123456789",
            "author": "示例用户",
            "text": "这是一条一级评论",
            "created_at": "2026-10-30T19:00:00+08:00",
            "likes": 2,
            "replies": 1
          }
        ]
      },
      "error": null
    }
  ],
  "duration_ms": 1500,
  "cost_yuan": 0
}
```

- `data` 与输入项顺序对应；每项先判断 `status`（`success` / `failed`），失败原因在 `error`。
- `result.total_comments` 为平台提供的评论总数，可能包含回复，未知时为 `null`；不能与一级评论条数直接比较。
- `result.comments` 的元素结构与 GET 相同；`result.likes` 是作品点赞字段，评论点赞在 `result.comments[].likes`。
- 传 `page` 时，`complete=true` 只表示该页没有报告不完整，不代表整篇作品全部评论已取完。
- 省略 `page` 时，`complete=true` 要求采集到末页且采集器能确认全部公开一级评论；`false` 可能是中途受限或平台无法证明完整，不等于已返回评论无效。
- 单项失败时，`status="failed"`、`complete=false`，`result` 各字段为 `null`，`error` 为原因。批量 HTTP 200 仍可能含失败项。
- `duration_ms` 为整批耗时（毫秒）；`cost_yuan` 为本次请求按新增代理 IP 数估算的费用（元）。

## 3. HTTP 错误与超时

| HTTP 状态 | 含义 |
|---|---|
| `422` | 参数缺失、平台或 URL 不匹配、`page` 不合法等；批量中可定位到单项的业务错误通常放在该项 `error` |
| `502` | GET 评论首次读取不可用，例如平台限制、验证码或采集失败 |
| `504` | 同步请求超过时限，默认 120 秒 |
| `500` | 未预期服务端错误 |

业务错误响应示例（原因随实际失败变化）：

```json
{
  "detail": "评论数据不可用"
}
```

参数格式校验错误的 `detail` 可能是数组，调用方应同时兼容字符串和数组。
批量链接或耗时较长的采集使用 `POST /api/v1/jobs`，具体提交、查询和结果获取方式见
[API 简明使用文档](API_QUICKSTART.md#4-批量获取互动和评论)。
