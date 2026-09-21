# Creator Engagement API 接口文档

最后更新：2026-09-21
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
| `POST` | `/api/v1/jobs` | 提交异步批量采集任务，返回 `job_id` |
| `GET` | `/api/v1/jobs/{job_id}` | 查询异步任务状态、进度、总耗时和总成本 |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | 取消排队中或运行中的异步任务 |
| `POST` | `/api/v1/jobs/{job_id}/resume` | 保留已有结果并续跑尚未完成的条目 |
| `GET` | `/api/v1/jobs/{job_id}/results` | 分页读取已完成的异步任务结果 |
| `POST` | `/api/v1/collect` | 同步批量采集，保留给兼容调用和小批次 |
| `GET` | `/api/v1/interactions` | 获取内容的互动统计 |
| `GET` | `/api/v1/comments` | 获取内容的一级评论正文 |
| `GET` | `/api/v1/health` | 获取服务和代理池运行状态 |
| `GET` | `/api/v1/authors/works` | 按 `author_id + media_name` 获取作者作品的一页数据 |
| `POST` | `/api/v1/authors/jobs` | 提交单个作者的异步作品采集任务 |
| `POST` | `/api/v1/authors/jobs/batch` | 批量提交作者采集任务 |
| `GET` | `/api/v1/authors/jobs/{job_id}` | 查询作者任务状态 |
| `GET` | `/api/v1/authors/jobs/{job_id}/results` | 分页读取作者任务结果 |
| `POST` | `/api/v1/authors/jobs/{job_id}/cancel` | 取消作者任务 |
| `POST` | `/api/v1/authors/jobs/{job_id}/resume` | 续跑作者任务 |

作者接口的请求参数、分页规则与各平台获取限制见 [作者内容 API](AUTHOR_CONTENT_API.md)。
作品列表接口开放不代表所有平台都已能获取作者的全部历史作品。

当前接口不要求业务鉴权 Header。批量接口使用 JSON 请求体；两个单项接口继续使用 URL Query。
生产部署仍应在网关层配置 HTTPS、访问控制、限流和请求日志脱敏。

服务启动后可以查看自动生成的接口定义：

```text
Swagger UI:  http://39.106.202.228:8200/docs
ReDoc:       http://39.106.202.228:8200/redoc
OpenAPI:     http://39.106.202.228:8200/openapi.json
```

### 2026-09-21 服务更新

8200 服务已于 2026-09-21 15:44（北京时间）更新，现有 URL、请求参数和响应结构保持兼容。
本次上线小红书目标笔记计数归属校验、评论空末页处理，微信公众号新版互动字段与回复数解析，
以及限定域名的代理 CONNECT 恢复。同步、异步批量条目数仍不设总数上限；同步请求总时限仍为 120 秒。

小红书、微信公众号、微信视频号的业务原始链接仍受后文列出的平台访问条件限制。
本次部署不代表 App 游客采集路径已打通；尚无可验证字段的请求仍按失败返回。

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
| `weibo` | `微博`、`新浪微博` |

服务会同时识别 URL 所属平台并校验 `media_name`。二者不一致时，单项 GET 接口返回 HTTP
422，批量接口将对应项标记为 `failed` 并继续处理其他项。微信公众号和微信视频号是两个
独立平台；响应中的“微信公众号”使用 `wechat`，视频号使用
`wechat_channels`。

请求可以继续使用英文名或兼容别名，所有业务响应中的 `media_name` 一律返回规范中文名：
`抖音`、`今日头条`、`微信公众号`、
`微信视频号`、`小红书`、`好看视频`、`快手`、`哔哩哔哩`、`微博`。输入仍接受
`微信`、`微信公众号` 和 `B站` 等兼容别名。`新浪微博` 与 `微博`、`weibo` 使用同一个采集器，
适用于互动、评论、同步批量和异步任务接口，响应名称统一为 `微博`。

微信视频号支持以下 URL：

- `https://weixin.qq.com/sph/...`
- `https://channels.weixin.qq.com/finder-preview/pages/sph?id=...`
- `https://channels.weixin.qq.com/finder-preview/pages/feed?eid=...&token=...`
- `https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?...extInfo.feedID=export/...`

前三类公开分享 URL 可匿名读取页面公开互动量。最后一类客户端跳转 URL 不公开视频数据，互动量
和评论正文都必须使用授权 Windows 微信侧车。未配置或无法连接侧车时，单项接口返回 HTTP 502，
批量项为 `failed`；服务不会查询其他项目或缓存型数据源，也不会把空值伪装为成功。

微博正文支持 `weibo.com/{uid}/{bid}` 和 `m.weibo.cn/detail/{mid}`；视频支持
`video.weibo.com/show?fid=1034:{id}` 与 `weibo.com/tv/show/1034:{id}`。视频 `fid` 不是微博
MID，服务会在同一代理租约中建立匿名访客会话，再通过微博公开组件接口完成映射。已删除或无
查看权限的正文会快速返回明确失败，不会继续进行无效浏览器重试。

## 3. 批量采集

新接入的外部服务推荐使用异步任务接口。调用方先提交任务获得 `job_id`，再轮询状态并使用
cursor 分页读取已完成结果；不需要让一个 HTTP 请求一直等待整批采集结束。原
`POST /api/v1/collect` 继续保留，适合小批次和已有调用方。

异步与同步接口共用同一套平台适配器、代理池、缓存和统一返回结构。接口不使用流式 JSON。

### 3.1 提交异步任务

```http
POST /api/v1/jobs
Content-Type: application/json
Idempotency-Key: hhm-20260828-001
```

```json
{
  "items": [
    {
      "item_id": "source-row-001-interactions",
      "url": "https://www.douyin.com/video/1234567890",
      "media_name": "抖音",
      "type": "interactions"
    },
    {
      "item_id": "source-row-001-comments",
      "url": "https://www.douyin.com/video/1234567890",
      "media_name": "抖音",
      "type": "comments"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | array | 是 | 采集项列表，至少一项，默认不限制条目总数 |
| `items[].item_id` | string | 是 | 调用方生成的批次内唯一标识，用于将异步结果关联回原始记录 |
| `items[].url` | string | 是 | 受支持的内容 URL |
| `items[].media_name` | string | 是 | 支持中文、英文规范值和兼容别名；响应统一为中文规范值 |
| `items[].type` | string | 是 | `interactions` 或 `comments` |
| `items[].page` | integer/null | 否 | 评论项不传时获取全部当前可见一级评论；传数字时只获取对应页，必须大于等于 1 |
| `webhook_url` | string/null | 否 | 任务结束通知地址，必须为 HTTPS 且域名已加入服务端白名单 |

同一任务内的 `item_id` 不能重复。`page` 必须是 JSON 数字，字符串形式的 `"1"` 不接受。
互动量和评论是两个独立采集项；同一 URL 同时需要两类数据时，应像示例一样提交两项并使用不同
`item_id`。

`Idempotency-Key` 是可选请求头，长度为 1 到 128。生产调用建议每个业务批次都传入：相同 Key
和相同请求体重复提交会返回原 `job_id`，不会重复执行；相同 Key 对应不同请求体时返回 HTTP
409。重试时请求体和 Key 都必须保持不变。

提交成功返回 HTTP 202：

```json
{
  "job_id": "job_1d8d5e1726464a6492bb79246fa6e6fd",
  "status": "queued"
}
```

`queued` 表示服务已经接收并持久化任务，不表示采集已经完成。

### 3.2 查询任务状态

```http
GET /api/v1/jobs/job_1d8d5e1726464a6492bb79246fa6e6fd
```

```json
{
  "job_id": "job_1d8d5e1726464a6492bb79246fa6e6fd",
  "status": "completed",
  "progress": {
    "total": 2,
    "completed": 2,
    "success": 2,
    "failed": 0
  },
  "duration_ms": 4218,
  "cost_yuan": 0.00168,
  "created_at": "2026-08-28T08:10:00Z",
  "started_at": "2026-08-28T08:10:01Z",
  "finished_at": "2026-08-28T08:10:05Z",
  "webhook_status": null,
  "error": null
}
```

任务 `status`：

| 值 | 含义 |
|---|---|
| `queued` | 已接收，等待任务执行槽位 |
| `running` | 正在采集，已完成结果可以开始分页读取 |
| `completed` | 任务调度完成；具体项目仍可能有 `status=failed` |
| `failed` | 任务级执行中断；已经完成的项目仍可从结果接口读取 |
| `cancelled` | 调用方已取消任务；已经完成的项目仍可从结果接口读取 |

`progress.success` 和 `progress.failed` 是项目级计数。任务为 `completed` 只表示所有项目都已得到
明确结果，不代表每个项目都成功。任务运行期间 `duration_ms` 会随结果逐条更新，`cost_yuan`
在任务结束后返回本任务触发的新增代理采购成本。`error` 仅描述任务级失败或取消原因；项目级
原因仍读取结果中的 `error`。

轮询建议使用 2～5 秒间隔，并在网络错误时指数退避。不要为同一个任务并发启动多个状态轮询器；
状态查询不会加快采集，只会增加 API 和调用方自身的连接负担。

### 3.3 取消异步任务

```http
POST /api/v1/jobs/job_1d8d5e1726464a6492bb79246fa6e6fd/cancel
```

取消接口是幂等的：任务已是 `completed`、`failed` 或 `cancelled` 时，返回当前状态而不重复改变
结果。运行中的采集会被取消，已完成并落库的结果保留可读；尚未完成的项目不会被伪造为失败
结果。任务不存在或已过期时返回 HTTP 404。

```json
{
  "job_id": "job_1d8d5e1726464a6492bb79246fa6e6fd",
  "status": "cancelled",
  "progress": {
    "total": 500,
    "completed": 132,
    "success": 110,
    "failed": 22
  },
  "duration_ms": 182304,
  "cost_yuan": 0.00672,
  "created_at": "2026-08-29T08:10:00Z",
  "started_at": "2026-08-29T08:10:01Z",
  "finished_at": "2026-08-29T08:13:03Z",
  "webhook_status": null,
  "error": "任务已由调用方取消"
}
```

### 3.4 分页读取异步结果

首次读取：

```http
GET /api/v1/jobs/job_1d8d5e1726464a6492bb79246fa6e6fd/results?limit=100
```

后续读取时原样传回上一页的 `next_cursor`：

```http
GET /api/v1/jobs/job_1d8d5e1726464a6492bb79246fa6e6fd/results?limit=100&cursor=<next_cursor>
```

`limit` 默认为 100，范围为 1 到 500。结果按完成顺序返回，不保证与提交顺序一致，调用方必须
使用 `item_id` 关联原始记录。

```json
{
  "job_id": "job_1d8d5e1726464a6492bb79246fa6e6fd",
  "status": "running",
  "data": [
    {
      "item_id": "source-row-001-interactions",
      "url": "https://www.douyin.com/video/1234567890",
      "media_name": "抖音",
      "type": "interactions",
      "status": "success",
      "complete": true,
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
        "comments": null
      },
      "error": null,
      "duration_ms": 816
    }
  ],
  "next_cursor": "MQ==",
  "available_count": 1,
  "total": 2,
  "duration_ms": 816,
  "cost_yuan": 0
}
```

当任务仍为 `queued/running` 时，即使当前可用结果已经读完，接口仍返回一个非空
`next_cursor`，调用方应保存它并在稍后继续读取，不能自行解析或生成 cursor。任务进入
`completed/failed/cancelled` 且已读完全部结果后，`next_cursor` 才为 `null`。此时异步消费完成。

`available_count` 是当前已经完成并可读取的结果总数，`total` 是提交的项目总数。调用方可以在
任务运行期间持续消费新结果，也可以等待任务结束后再分页读取。

每个项目有独立的最大执行时间，超时后该项目返回 `status=failed`，`error` 会明确写出超时秒数，
其他项目继续执行。整批任务也有最大执行时间；触发后任务变为 `failed`，已完成结果仍然保留。
当前单项目默认上限为 90 秒，在取得平台和全局采集槽位后才开始；等待槽位不计入执行预算。
整批时限为 `min(86400, JOB_TIMEOUT_SECONDS × ceil(总条数 / JOB_TIMEOUT_BATCH_SIZE))`，
默认每 500 条给予 1800 秒，因此 2615 条允许执行 10800 秒。`JOB_TIMEOUT_SECONDS=0` 关闭整批
时限。排队等待任务槽位不消耗整批时限；等待平台/全局槽位仍消耗整批时限。
平台连续临时失败触发熔断时，排队项目会等待 20 秒冷却后再做真实请求，开始采集前的冷却等待
不占用单项目预算，也不会因为熔断处于打开状态而直接批量标记失败。默认触发阈值为连续 24 次临时失败；对抖音正式并发 4 而言相当于连续六轮全部失败，
避免单轮代理波动误触发平台级熔断。

单条异步结果默认最多序列化为 8 MiB；超过上限时该项目返回 `status=failed` 和明确错误，其他
项目继续执行。这个限制为 MongoDB 文档和 API 进程保留安全余量，不改变正常互动量或分页评论结果。

### 3.5 Webhook、持久化和过期

请求体传入 `webhook_url` 后，任务结束时服务会 POST 一次任务状态对象，失败时按配置重试。
Webhook 只通知状态和进度，不直接推送全部结果；接收方仍使用结果接口分页读取。出于 SSRF
防护，URL 必须使用 HTTPS，且目标域名必须配置在 `JOB_WEBHOOK_ALLOWED_HOSTS` 白名单中。

任务、结果、原始条目分别写入项目专用 MongoDB 的 `jobs`、`job_results`、`job_requests` 集合，
任务结束后保留 24 小时。每个原始条目单独存储，避免整个请求超过 MongoDB 单文档限制。
结果接口直接从 MongoDB 分页读取。任务进度与项目结果逐条提交；写入期间发生取消时，先完成
这次结果写入再退出，避免已有结果和进度不一致。

服务启动会恢复因重启/关闭中断的任务：从已落库结果恢复进度，跳过已有结果的 `item_id`，继续
未完成条目。已产出失败结果的条目也会保留，不在续跑时重新采集。主动取消和超过整批时限的
任务不自动恢复，可在保留期内显式调用：

```http
POST /api/v1/jobs/{job_id}/resume
```

返回 HTTP 202 和原 `job_id`；重复调用不会启动第二个执行实例，原有结果的分页游标仍有效。
已完成任务直接返回当前状态。任务不存在或已过期返回 HTTP 404；旧版本未保存完整原始条目的
历史任务返回 HTTP 409，调用方需要用原始未完成条目和新的 `Idempotency-Key` 重新提交。
`JOB_STORE_BACKEND=sqlite` 保留相同的续跑行为。

成功率应区分 `progress.success / progress.total`（整批交付）和
`progress.success / progress.completed`（已完成条目）。未完成数为
`progress.total - progress.completed`；任务状态为 `completed` 不保证每条采集成功。

### 3.6 同步批量接口（兼容）

```http
POST /api/v1/collect
Content-Type: application/json
```

同步请求项不需要 `item_id`：

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

服务并发执行批次内项目，全部结束后一次返回完整 JSON：

```json
{
  "data": [
    {
      "url": "https://www.douyin.com/video/1234567890",
      "media_name": "抖音",
      "type": "interactions",
      "status": "success",
      "complete": true,
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
        "comments": null
      },
      "error": null
    }
  ],
  "duration_ms": 1830,
  "cost_yuan": 0.00084
}
```

同步 `/collect` 和异步 `/jobs` 默认不限制条目总数，5925 条可以一次提交。
`API_SYNC_MAX_ITEMS=0`、`JOB_MAX_ITEMS=0` 表示不限制；只有配置为正整数后才对超限请求返回 422。
条目总数不会提高已有的采集并发，但会增加排队、存储和总处理时间。
`/collect`、`/interactions` 和 `/comments` 的总时限默认均为 120 秒
（`API_SYNC_TIMEOUT_SECONDS`），包含排队、重试和评论分页。到期会取消本次等待，
不再有调用方等待的采集会一并取消，返回 HTTP 504 和改用 `/jobs` 的提示。
同步超时响应不包含部分结果，需要保存中间进度的调用应使用异步任务。
这些限制不改变异步任务的条目数和执行时限。

HTTP 响应包含 `X-Request-ID`。发生 422、504 或其他错误时，调用方应保存状态码、
响应 `detail` 和该请求编号，不能继续按提交成功处理。服务端日志以请求编号关联
请求开始、结束、耗时、校验字段及任务提交后的 `job_id`，不记录请求体、查询参数或 Cookie。

每一项始终包含相同的 `result` 字段。平台未公开的互动字段返回 `null`；未请求评论时
`comments` 为 `null`；评论请求成功且没有可见评论时为 `[]`。

- `status=success`：该项拿到了可验证的业务数据；
- `status=failed`：该项没有可用数据，固定结果字段均为 `null`，原因写入 `error`；
- `complete=true`：已经完成调用方指定的请求范围；
- `complete=false`：请求成功并返回了真实数据，但平台登录门槛或风控阻止了全量覆盖。

`status` 只表示请求成功或失败，不再使用容易被误解为失败的 `partial`。`complete` 只描述覆盖
范围。互动请求中平台本身不公开或不适用的字段为 `null`，不影响 `complete=true`；指定数字页
成功返回该页也属于 `complete=true`。默认全量评论只有确认到达公开末页才为 `true`，小红书
匿名游客首批、微博深页阻断等情况为 `false`。

同步响应和异步任务结果使用相同的项目字段。`duration_ms` 是整个批次的端到端耗时。
`cost_yuan` 当前仅统计本批次触发代理池新增 IP 的
采购成本，公式为 `新增 IP 数 × 0.00084 元`；复用已有代理和缓存命中不会产生新增
代理成本。它不是对客户的商业收费金额。

统一互动字段的解析覆盖如下；表中支持项以链接和平台会话有效为前提，不代表业务样例已通过在线验收。
实际页面没有公开某个值时仍返回 `null`：

| 平台 | views | likes | total_comments | shares | favorites | coins | danmaku | reposts | recommendations | comments |
|---|---|---|---|---|---|---|---|---|---|---|
| 抖音 | 支持 | 支持 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 部分作品 | 支持 |
| 今日头条 | 支持 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 |
| 微信 | 取决于授权会话 | 取决于授权会话 | 页面明确预载的精选评论计数或授权会话 | 取决于授权会话 | 自有号可用 | 不支持 | 不支持 | 不支持 | 自有号可用 | 严格匿名不可用 |
| 微信视频号 | 公开预览不提供；取决于授权侧车返回 | 支持 | 支持 | 支持 | 公开页支持 | 不支持 | 不支持 | 不支持 | 不支持 | 授权侧车可用 |
| 小红书 | 不支持 | 支持 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 游客浏览器可尝试公开首批，未保证可用；有效账号会话与 token 可尝试深分页 |
| 好看视频 | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 |
| 快手 | 支持 | 支持 | 支持 | 当前未公开 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 |
| 哔哩哔哩 | 视频/专栏可用 | 支持 | 支持 | 支持 | 支持 | 支持 | 视频可用 | 专栏可用 | 不支持 | 支持 |
| 微博 | 不支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 支持 | 不支持 | 支持但深页可能受限 |

## 4. 获取互动统计

`url` 可直接使用业务文件中的小红书 `/discovery/item/{id}` 或公众号 HTTP 链接；
适配器内部完成路径转换或 HTTPS 请求，批量结果仍保留调用方原始 URL。
小红书路径兼容不代表能自动取得缺失的 `xsec_token`，视频号客户端 `feedID` 链接的
授权客户端条件也不变。

公众号新版页脚若明确下发计数，解析器会把 `old_like_count` 映射为 `likes`（赞），
把 `like_count` 映射为 `recommendations`（推荐），把 `collect_count` 映射为 `favorites`。
特殊“助力”按钮的计数不作为推荐量；空字符串模板占位保持 `null`，明确的数字 0 保留为 0。
这表示字段解析能力，原始样例的无账号请求目前仍未稳定取得这些计数。

任意一个可验证的互动字段即可构成成功响应，其他字段为 `null`。公众号仅有明确的
正数 `preload_comment_total_cnt` / `elected_comment_total_cnt` 时会返回该精选评论计数，
不将预载数组长度当总数，也不把匿名页面的零值占位当真实零评论。
单项响应使用 `data.comments`，批量响应使用 `result.total_comments`，两者都是计数。

2026-09-21 在线抽样核验：从 `.local/top20_platform_data.json` 各选一个小红书、微信、微信视频号原始链接，
线上 8200 与当前工作区版本均返回 HTTP 502。对应原因分别为缺少有效 `xsec_token`、公众号访问环境验证码、
视频号客户端跳转链接缺少可用客户端会话。公众号仅要求评论数也尚未通过这次样例验收。
带 token 的小红书独立对照返回点赞、评论、分享、收藏四项计数；视频号 `sph` 对照此前成功，
本次请求失败，尚不能保证稳定性。对照链接不是业务原始样例，不能替代业务验收。
脱敏请求结果位于 `.local/analysis/interaction-h5-20260920/status-20260921.json`。

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
  "media_name": "哔哩哔哩",
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
  "media_name": "哔哩哔哩",
  "comments": [
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
  "media_name": "哔哩哔哩",
  "comments": []
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
  "proxy_mode": "required",
  "proxy_configured": true,
  "reliability_mode": "enterprise",
  "protocol_max_attempts": 3,
  "proxy_pool_size": 8,
  "proxy_max_concurrency": 1,
  "collection_max_concurrency": 8,
  "douyin_max_concurrency": 4,
  "toutiao_protocol_max_attempts": 1,
  "douyin_protocol_max_attempts": 3,
  "browser_max_concurrency": 3,
  "browser_max_attempts": 3,
  "browser_geoip_enabled": false
}
```

健康检查不是业务数据接口，因此不使用 `data` 外壳。
生产服务固定要求 `proxy_mode=required`：协议请求和浏览器兜底都必须先取得代理租约；代理池
不可用时采集项明确失败，不允许回退服务器本机 IP。代理供应商 API、授权侧车和 Webhook 属于
控制面或业务回调，不经过内容采集代理池。

## 7. 错误响应

批量请求的 JSON 结构、`type` 或 `page` 不合法时返回 HTTP 422；请求体合法后，单项 URL、
媒体或采集失败写入该项的 `status/error`，不会让其他项失败。

| HTTP 状态 | 场景 |
|---:|---|
| `404` | 异步任务不存在或已超过结果保留时间 |
| `409` | 相同 `Idempotency-Key` 已用于不同的任务请求 |
| `422` | 缺少参数、异步项缺少或重复 `item_id`、无效 cursor、Webhook 不合规、`page < 1` 或媒体参数不合法 |
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

下游应先判断 HTTP 状态码。评论接口的 `comments: []` 表示成功确认当前请求
没有返回一级评论；它与 HTTP 502 的“无法获取评论”含义不同。

## 8. Python 调用示例

异步批量调用：

```python
import time
from uuid import uuid4

import requests


base_url = "http://39.106.202.228:8200"
batch_key = f"engagement-{uuid4()}"
items = [
    {
        "item_id": "source-row-001-interactions",
        "url": "https://www.bilibili.com/video/BVxxxxxxxxxx",
        "media_name": "B站",
        "type": "interactions",
    },
    {
        "item_id": "source-row-001-comments",
        "url": "https://www.bilibili.com/video/BVxxxxxxxxxx",
        "media_name": "B站",
        "type": "comments",
    },
]

submitted = requests.post(
    f"{base_url}/api/v1/jobs",
    json={"items": items},
    headers={"Idempotency-Key": batch_key},
    timeout=30,
)
submitted.raise_for_status()
job_id = submitted.json()["job_id"]

while True:
    status_response = requests.get(
        f"{base_url}/api/v1/jobs/{job_id}",
        timeout=30,
    )
    status_response.raise_for_status()
    job = status_response.json()
    if job["status"] in {"completed", "failed", "cancelled"}:
        break
    time.sleep(2)

results = []
cursor = None
while True:
    params = {"limit": 100}
    if cursor is not None:
        params["cursor"] = cursor
    result_response = requests.get(
        f"{base_url}/api/v1/jobs/{job_id}/results",
        params=params,
        timeout=30,
    )
    result_response.raise_for_status()
    page = result_response.json()
    results.extend(page["data"])
    cursor = page["next_cursor"]
    if cursor is None:
        break

result_by_item_id = {item["item_id"]: item for item in results}
duration_ms = job["duration_ms"]
cost_yuan = job["cost_yuan"]
```

提交请求超时或网络中断时，使用相同 `Idempotency-Key` 和完全相同的请求体重试。只有在确认原
任务为 `failed` 且需要重新执行时，才生成新的 Key。

单项同步调用：

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
interaction_payload = interaction_response.json()
media_name = interaction_payload["media_name"]
stats = interaction_payload["data"]

comment_response = requests.get(
    f"{base_url}/api/v1/comments",
    params={"url": content_url, "media_name": "哔哩哔哩"},
    timeout=300,
)
comment_response.raise_for_status()
comment_payload = comment_response.json()
media_name = comment_payload["media_name"]
comments = comment_payload["comments"]
```

互动量和评论是两个独立业务请求，分别执行并缓存。默认全量评论可能需要多次平台上游请求，
需要长时间分页时应使用异步任务；单纯增加客户端超时不会改变服务端的同步总时限。
