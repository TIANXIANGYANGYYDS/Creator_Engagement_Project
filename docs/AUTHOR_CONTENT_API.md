# 按 author_id 和 media_name 获取公开作品内容

接收动态 `author_id` 和 `media_name`，支持抖音、快手、微博、B 站、小红书、今日头条，并兼容先前的新浪博客。返回视频标题、简介、作品链接、平台提供的媒体地址，以及文章正文；视频文案不包含语音转写。不依赖股票项目的固定作者名单。

## 创建作者任务

```bash
curl -X POST 'http://127.0.0.1:8200/api/v1/authors/jobs' \
  -H 'Content-Type: application/json' \
  -d '{"author_id":"3402362465301307","media_name":"今日头条"}'
```

标准参数为 `author_id`、`media_name`，均使用字符串。仍兼容旧参数 `uid`、`platform`。状态和列表响应增加 `author_id`，保留原有 `uid`。请求立即返回 HTTP 202 和 `job_id`，后台遍历平台允许访问的历史列表。**不限制作品总数，不设置最近几天的时间窗口。**

| 平台名称 | author_id 格式 |
| --- | --- |
| `douyin` / 抖音 | 数字 UID、抖音号或主页 `MS4wLj...` 格式的 sec_uid；公开账号通过搜索精确匹配，拒绝仅按昵称猜测 |
| `kuaishou` / 快手 | 接受数字账号及主页 `/profile/` 后的用户 ID；数字 ID 可通过已提供作品链接核验并缓存主页 ID，每次新采集重新核验；未建立映射时尝试平台原始 ID |
| `weibo` / 微博 / 新浪微博 | 数字 UID |
| `bilibili` / B站 / 哔哩哔哩 | 数字 mid |
| `xiaohongshu` / 小红书 / xhs | 小红书号或主页 `/user/profile/` 后的 24 位十六进制 ID；小红书号搜索需要有效会话 |
| `toutiao` / 今日头条 / 今日头条微头条 | 数字 UID 或主页 `/c/user/token/` 后的 token |
| `sina_blog` / 新浪博客 | 数字博主 UID，兼容旧接口 |

## 批量提交

`POST /api/v1/authors/jobs` 同时接受单个对象和对象数组；数组也可以提交到 `POST /api/v1/authors/jobs/batch`。

```json
[
  {"author_id": "3402362465301307", "media_name": "今日头条"},
  {"author_id": "ChenNan12.24", "media_name": "抖音"},
  {"author_id": "7946463148", "media_name": "新浪微博"}
]
```

返回 HTTP 202，包含 `total`、`accepted_count`、`duplicate_count`、`rejected_count` 和逐条 `data`。每条记录有原始位置 `index`、`author_id`、规范化的 `media_name`、`status`、`job_id` 或 `error`。同批按规范化平台和账号去重，重复记录返回同一个 `job_id`。不同批次可以再次提交同一作者。

不设作者数量总上限，也不设代理使用总次数上限；数据库分块写入、后台按并发容量排队。空 ID、非字符串 ID、不支持的平台或多余字段逐条拒绝，其他有效记录正常入队。单个对象的非法参数仍返回 HTTP 422。

真实文件 `.local/top20_platform_data.json` 含 10,325 条作品记录及 20 个平台。提取两项请求参数后，六个平台的有效记录为 4,791 条，去重得到 3,924 位作者；另有 49 条缺失 ID，5,485 条属于本接口范围外的平台。检查结果已保存到 `.local/analysis/author-real-data/input-validation.json`；可提交的去重请求已生成到 `.local/analysis/author-real-data/authors.request.json`，原始文件未修改，未自动创建这 3,924 个全历史任务。

```bash
curl -X POST 'http://127.0.0.1:8200/api/v1/authors/jobs/batch' \
  -H 'Content-Type: application/json' \
  --data-binary @.local/analysis/author-real-data/authors.request.json
```

原始文件中的 `author_name`、`url` 不属于请求参数。调用前应投影出 `author_id`、`media_name`；接口不会把文件中的历史作品 URL 当作作者全集。

## 列表与详情分开执行

1. 每个作者有一个列表采集流程。同一租约有效时，连续分页复用同一个 IP 和 Cookie 会话。每页发现的作品 ID、公开链接和取详情所需的上下文立即存入 MongoDB，完成检查点后继续翻页。
2. 默认每个作者有 **4 个详情 worker**，从 MongoDB 原子领取待处理作品，使用各自的 HTTP 会话和代理租约采集。列表会话与详情 worker 的上下文相互独立。已由列表返回完整公开视频文案和链接的作品可以直接完成，避免重复请求。
3. 列表会话使用超过 90 秒，或代理可用时间少于 40 秒时，重建会话并继续原游标。接近到期的代理被淘汰。每页最多三次即时尝试；临时失败后任务进入退避重试，保留当前游标。明确登录限制作为不可用入口报告。失败详情持久化为 failed，供续跑。
4. 浏览器与协议请求可以组合使用。浏览器也使用强制代理，随租约结束关闭，并与原互动接口共用浏览器并发预算。

最多同时执行 2 个作者任务；与互动采集共享全局采集并发和平台并发。当前生产代理池 8 个端点，单端点并发 1，因此并行详情由不同的可用代理租约执行；成功租约可以在有效期内再次使用，无需每篇都购买新 IP。

每页发现、每篇详情默认最多 120 秒（包含等待资源），单次作者任务最多执行 24 小时。时限是资源保护，不是作品数量上限。超时后可续跑。

## 进度、结果、取消和续跑

```bash
curl 'http://127.0.0.1:8200/api/v1/authors/jobs/<job_id>'
curl 'http://127.0.0.1:8200/api/v1/authors/jobs/<job_id>/results?limit=100'
curl --get 'http://127.0.0.1:8200/api/v1/authors/jobs/<job_id>/results' \
  --data-urlencode 'limit=100' --data-urlencode 'cursor=<next_cursor>'
curl -X POST 'http://127.0.0.1:8200/api/v1/authors/jobs/<job_id>/cancel'
curl -X POST 'http://127.0.0.1:8200/api/v1/authors/jobs/<job_id>/resume'
```

| 状态字段 | 含义 |
| --- | --- |
| `status` | queued、running、completed、partial、failed、cancelled |
| `phase` | queued、discovery（列表与详情可同时运行）、retry_wait（等待自动重试）、details、finished |
| `retry_count` / `next_retry_at` | 已发生的任务级重试次数及下一次重试时间；不包括页内即时尝试 |
| `saved_count` | 已发现并去重保存的作品数，包括待处理详情 |
| `detail_pending_count` | 等待或正在获取详情的数量 |
| `detail_completed_count` | 已取得所需公开内容的数量 |
| `detail_failed_count` | 本次尝试后详情仍失败的数量 |
| `pages_completed` | 已完成检查点的来源页数 |
| `discovery_done` | 当前来源明确返回无下一页；不表示平台历史全集可见 |
| `complete` | 仅当来源覆盖完整、列表结束、所有详情完整时为 true |
| `error` | 失败、覆盖不完整或取消原因 |
| `expires_at` | 沿用 JOB_RESULT_TTL_SECONDS，当前为结束后 24 小时 |

结果每页 `limit` 范围 1–500。运行中只返回按发现顺序连续完成的结果；较早的作品还在获取详情时，暂缓游标推进，防止调用方漏掉后完成的作品。没有新增结果时仍返回 `next_cursor`，稍后用相同游标继续读取。任务停止后也可以读取待处理作品的摘要。

`resume` 会从已保存的列表游标继续，并重试 pending/failed 详情。**即使列表已经结束，也能仅重试失败详情，不重新抓列表。** 若列表结束且没有待处理/失败详情，只是平台无法证明历史完整，续跑返回 409；需要重新发现作品时新建任务。

续跑会更新已有作品的内容；若此前已经读取过失败或待处理摘要，续跑完成后从结果第一页重新读取，按 work_id 更新本地记录。结果游标不是内容更新订阅。

进程重启会自动恢复 queued/running 任务和领取中断的详情；主动取消后必须显式续跑。列表临时失败时，已发现作品的详情继续处理，然后自动等待重试；明确终止错误或达到运行时限后以 partial/failed 结束。取消和关机等待正在写入的检查点完成。

## 单页接口

```bash
curl --get 'http://127.0.0.1:8200/api/v1/authors/works' \
  --data-urlencode 'author_id=3402362465301307' --data-urlencode 'media_name=今日头条'
```

后续页加 `cursor=<next_cursor>`，保持同一 UID 和平台。单页也将列表和详情分开领取代理、并发获取详情，但跨 HTTP 请求不保留列表会话。**需要连续历史分页时优先使用任务接口。**

返回 `data`、`next_cursor`、`coverage`、`reason`。来源页大小由平台决定。`coverage=complete` 仅说明本页来源可用于判断覆盖范围，不代表所有历史已完成。非法参数/游标返回 422，上游拒绝或缺少列表字段返回 502，本页超时返回 504。不会把缺失列表或登录验证页当作成功的空列表。响应带 `X-Request-ID`。

作品字段：`work_id`、`author_id`、`author_uid`（兼容字段）、`resolved_author_uid`（解析出的平台标识）、`author_name`、`media_name`、`content_type`、`url`、`title`、`description`、`text`、`published_at`、`cover_url`、`media_urls`、`duration_ms`、`content_complete`、`detail_status`（pending/complete/failed）、`error`、`fetched_at`。媒体地址可能过期，视频没有可取得的直链时 media_urls 可以为空。小红书保留列表下发的笔记 xsec_token，不能仅凭作品 ID 重新生成。

## 当前真实验证与限制（2026-09-19）

| 平台 | 已实现路径与本次验证 |
| --- | --- |
| 抖音 | 仅访客浏览器初始化会话、原生签名及网页请求捕获。`ChenNan12.24` 首屏 20 条，`46219502311` 首屏 23 条；后续页出现缺少列表的状态包或连接失败。此前 v4 的 61 页/1,117 条使用了账号 Cookie，不能当作无账号通过的证据。 |
| 微博 | 访客移动时间线。`7002783993` 首屏 10 条，重复读取首屏成功，但当前第二页失败；`7727451889` 返回明确登录提示。后者按访问限制报告，不当作空账号或完整末页。 |
| B 站 | 访客设备 ID + WBI，补齐公开 Web 投稿参数。部署后真实任务分别取得 `1804532025` 的 6 页、180 条，以及 `1187120568` 的 5 页视频投稿、143 条；323 条详情全部完成，且核验后一作者专栏数为 0。仍有 `-352/412`，需换代理续页；动态全集尚未完成验收，限时测试已主动取消并保留结果。 |
| 小红书 | 访客浏览器、精确账号解析、签名接口和网页正文。实际主页能连接，但返回未登录页，不能从其中的空列表推断没有作品；账号搜索/列表也出现连接失败，未通过真实列表验收。 |
| 快手 | REST/GraphQL 列表、移动分享页详情、公开作品核验作者映射。仅访客真实列表仍返回 50 或连接失败；直播主页公开入口也未得到可用作品列表，尚未通过。 |
| 今日头条 | 数字 ID 跳转主页 token、综合列表、独立详情。部署后的纯访客任务对 `3402362465301307` 连续读取 25 页、保存 245 条视频，所需公开内容均完成，当前来源明确结束；结果读取 245 条且无重复。另一个混合作者已抽检成功 10 篇正文，但仍有失败/待处理详情。仍报告 partial 覆盖，不宣称平台隐藏作品也已取得。 |
“支持接入”不代表六个平台均可在当前 IP、会话条件下稳定读取完整历史。六个平台的公开来源目前保守报告 partial；即使读到了当前末页，也不宣称包括已隐藏、删除、私密或账号权限不可见的作品。

## 存储与部署

使用本项目 MongoDB 的 `author_jobs`、`author_works`、`author_identities`，与互动任务集合分开。作品按 job_id + work_id 去重，待处理详情、内部访问上下文和领取状态独立保存。不会把作者全部作品装入一个 MongoDB 文档或一个内存列表。升级启动时迁移旧作品的详情状态；兼容原作者任务 API。作者映射仅保存公开标识、核验时间及可选来源作品 ID，不保存 Cookie。每个新任务或独立单页请求重新核验映射；账号改名或身份不符会使映射失效。同一作者任务中的代理切换保留已核验的映射和分页游标。

复用现有 curl_cffi、Camoufox/Playwright、xhshow 依赖。当前没有账号配置，作者采集使用全自动访客 Cookie，默认 `AUTHOR_GUEST_ONLY=true`，优先于所有作者账号配置和小红书专用 Cookie 模式：不读取已保存的登录状态、localStorage，不向作者请求注入账号 Cookie。只保留同一代理租约内新建的访客会话。保持一个 Uvicorn worker，避免多进程重复恢复作者任务。

```dotenv
AUTHOR_GUEST_ONLY=true
AUTHOR_USE_SAVED_SESSIONS=false
DOUYIN_AUTHOR_SESSION_MODE=anonymous
```

健康接口应显示 `author_pipeline_version=7`、`author_guest_only=true`、`author_use_saved_sessions=false`、`douyin_author_session_mode=anonymous`。部署文件遗留 Cookie 不影响这三个有效模式值；修改配置需要重启。无需扫码、人工登录或传入账号 Cookie。`guest_only` 并非禁用 Cookie；同一代理下由平台下发的访客 Cookie 正常使用。自动获取 Cookie 的研究和实测见 [v7 验收记录](AUTHOR_AUTO_SESSION_2026-09-19.md)。

列表每页仍有 120 秒超时和最多三次即时换代理尝试。任务遇到临时上游错误后，以 5、10、20、40、60 秒退避，从已保存游标重新建立访客会话继续；不设代理消耗次数或作品总数上限。等待不占作者执行槽、采集槽、浏览器或代理租约。状态中的 `phase=retry_wait`、`retry_count`、`next_retry_at` 和 `error` 可用于观察当前进度；此时可继续读取已完成的作品或取消任务。单次运行窗口为 24 小时，截止时间持久化，服务重启不会重置；主动 resume 开始新的运行窗口。失败详情仍可单独 resume。非法游标、明确身份不匹配或平台明确返回登录限制会停止本次列表遍历，不无限重复同一入口。

导入已有作品参考链接，以核验快手数字 ID 的主页映射：

```bash
python -m app.manually_execute_script.seed_author_references \
  .local/top20_platform_data.json --platform kuaishou
```

可用重复的 `--author-id` 选择抽检作者。只保存经在线作品 ID、数字作者 ID 双重验证的映射；不创建采集任务、不复制旧内容作新结果。新发现流程重新读取来源作品核验，游标中的任意主页 ID 不作为身份依据。

抖音从全新浏览器初始化访客 UIFID、可用的 s_v_web_id / msToken，使用现有签名及网页请求捕获。代理重建时重新取得访客会话，不携带账号 Cookie。来源作品映射导入在仅访客模式下也使用新浏览器核验；来源作品不可见时明确失败，不要求改成账号模式。

对已提供作品链接、但平台用户搜索没有收录的作者，可预先导入精确核验后的映射：

```bash
python -m app.manually_execute_script.seed_douyin_authors \
  .local/top20_platform_data.json \
  --author-id Xxy_0424 --author-id 34294266431
```

导入时必须同时核验作品 ID、作者公开账号及 sec_uid；不按昵称匹配。后续 HTTP 请求仍只传 author_id 和 media_name。没有映射且搜索不可见的新账号仍可能无法仅凭公开抖音号解析，此时明确失败。作品列表不可用时也不会把已知的一条作品伪装成作者完整历史。
