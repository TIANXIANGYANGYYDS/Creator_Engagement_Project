# 部署说明

项目使用 `MyAgent` Conda 环境，已验证 Python 3.13.12。

## 2026-11-01 当前发布

北京时间 03:37 已将代码提交 `5a16989` 发布到 8200，包含微博/B站评论修复和 24 个来源扩展，注册来源共 85 个。全量测试 919 passed、22 skipped；公网地址健康检查及微博、B站、澎湃、AcFun 评论抽查通过，不表示所有来源、所有历史评论均可采集。

API 从 `.local/releases/20261101-033649-all-85-platforms/source` 的提交快照运行，保持单 worker；该目录的 `.local` 链接到主项目运行数据目录，沿用原配置、日志和会话。修改开发工作区不会自动更新运行实例，后续发布应准备新快照后切换。当前发布及重启证据位于 `.local/analysis/comments-release-20261101/`。

重启前后 6 个已完成任务、6,755 条输入和结果数量一致，无活动任务；小红书 `xhs-session-owner` 保持原进程。代理池参数仍为 8 个 IP、单 IP 并发 1，临期租约问题留待后续修复。

## 2026-10-31 当前部署参数

保留 `COLLECTION_MAX_CONCURRENCY=8`、`PROXY_POOL_SIZE=8`、`PROXY_MAX_CONCURRENCY=1`、`BROWSER_MAX_CONCURRENCY=3`、`JOB_ITEM_MAX_CONCURRENCY=16`。新增 `PUBLIC_PLATFORM_MAX_CONCURRENCY=1`（可设 1–4），它只控制普通发布方，同协议的多个来源共享配额。当前实测不支持提高单 IP/发布方并发，比较结果见 [本轮验收](docs/PLATFORM_EXPANSION_2026-10-31.md)。

小红书保持 `XIAOHONGSHU_SESSION_MODE=stored`、`XIAOHONGSHU_REQUEST_INTERVAL_SECONDS=15`，已有专用会话进程通过本机 IP 请求，不使用代理池。API 更新不得重新登录、替换 Profile 或重启 `xhs-session-owner`。账号的每次上游请求受独立串行及间隔约束。

批任务现按上游分组调度，限速等待不占全局槽位；每条执行超时从取得必要容量后计算。更新前检查活动任务和 `job_requests` 完整性，保留已写入结果；关闭/重启中断的任务启动后自动恢复，主动取消的任务不会自动恢复。保持单个 API worker，不能同时运行两个连接同一任务库的生产实例。

## 配置

```bash
cp .env.example .local/env/.env
```

部署时将真实的 `PROXY_51_API_URL`、平台会话 Cookie 等值写入
`.local/env/.env`。该目录已被 Git 忽略，不应提交或打印敏感值。

异步任务默认使用项目专用 MongoDB：

```dotenv
JOB_STORE_BACKEND=mongodb
MONGO_URI=mongodb://<user>:<password>@<host>:27017/?authSource=creator_engagement
MONGO_DB_NAME=creator_engagement
JOB_RESULT_TTL_SECONDS=86400
JOB_MAX_ITEMS=0
JOB_RESULT_MAX_BYTES=8388608
JOB_TIMEOUT_SECONDS=1800
JOB_TIMEOUT_BATCH_SIZE=500
JOB_XIAOHONGSHU_ITEM_TIMEOUT_SECONDS=300
REQUEST_CONNECT_TIMEOUT_SECONDS=5
DOUYIN_PROTOCOL_MAX_ATTEMPTS=8
```

Mongo 用户只需对 `creator_engagement` 库具有 `readWrite` 权限。升级后会自动创建
`job_requests` 的分页和 TTL 索引。服务启动自动续跑关闭/重启中断的任务；主动取消和整批超时
任务通过 `POST /api/v1/jobs/{job_id}/resume` 显式续跑。旧 MongoDB 任务没有保存原始输入，
不能恢复缺失条目，需由调用方提供原请求重新提交。

单条 90 秒预算不再包含等待采集槽位；整批时限按每 500 条分配 1800 秒（向上取整），最大
24 小时。默认连接建立上限 5 秒，整个 HTTP 请求仍为 20 秒；抖音最多尝试 8 次，每次失败后
更换代理和访客会话。保留 1 个 Uvicorn worker，避免多个进程同时恢复同一任务。

首次迁移前先停止新任务并执行：

```bash
conda run -n MyAgent python -m app.manually_execute_script.migrate_jobs_to_mongo
conda run -n MyAgent python -m app.manually_execute_script.migrate_jobs_to_mongo --apply
```

脚本只迁移保留期内的数据并创建所需唯一索引、分页索引和 TTL 索引。需要回滚时设置
`JOB_STORE_BACKEND=sqlite`，原 SQLite 文件不会被迁移脚本删除。

浏览器兜底保留 Cookie 和登录状态，但不再启用磁盘 HTTP 缓存，避免 Profile 随采集量持续膨胀。
升级前已经生成的 `cache2` 是可重建数据；如需回收其磁盘空间，应先停止服务并单独备份或清理
这些缓存目录，不要删除整个 Profile。

以下是历史低内存示例，并非当前生产参数。低于约 2 GB 可用内存时，可评估：

```dotenv
BROWSER_MAX_CONCURRENCY=1
COLLECTION_MAX_CONCURRENCY=4
PROXY_POOL_SIZE=2
PROXY_MAX_CONCURRENCY=1
ENGAGEMENT_CACHE_TTL_SECONDS=120
ENGAGEMENT_CACHE_MAX_ENTRIES=1000
ENGAGEMENT_CACHE_MAX_BYTES=67108864
```

Uvicorn 建议只启动 1 个 worker；每增加一个 worker 都会复制浏览器并发槽位、缓存和代理池，
不能靠多 worker 提高本项目吞吐。容量和成本估算见 `docs/COST_AND_CAPACITY.md`。

生产服务仅接受 `PROXY_MODE=required`，普通平台没有可用代理即明确失败。小红书通过专用账号策略本机直连；不能为了它把全局代理模式改为 `direct` 或 `prefer`。底层客户端保留其他模式用于隔离测试，不能作为生产配置建议。

应用诊断日志写入 `.local/logs/api.log`，每个文件 10 MiB，保留 3 个轮转备份。
日志记录请求阶段、异常类型、curl 错误码、耗时和批次完成计数，不记录签名查询参数或 Cookie。
HTTP 请求的开始、结束、响应状态、耗时和 `X-Request-ID` 也写入该轮转日志；
422 参数校验记录字段位置和错误类型（最多前 20 项），不记录输入值。
任务提交记录关联 `request_id` 和 `job_id`。日志中的 `response_complete` 表示应用已
完成响应发送，不代表调用方已完成业务处理。

同步采集默认 `API_SYNC_TIMEOUT_SECONDS=120`、`API_SYNC_MAX_ITEMS=0`。
同步和异步的条目数配置为 0 表示不限制总数；设为正整数时才对超限批次返回 422。
升级旧部署时，将 `.local/env/.env` 中原有的 `JOB_MAX_ITEMS=5000` 改为 `0`。
采集并发仍由 worker、平台及全局槽位控制。同步总时限包含排队、重试和评论分页，超过返回 504。
大批量及长时间评论采集使用 `/api/v1/jobs`，其执行时限不受同步设置影响。
MongoDB 单次操作配置 10 秒客户端总时限，避免任务提交和查询无限等待数据库响应。

## API

小红书原始 URL 的内部访问链接恢复支持当前来源查询：配置
`XIAOHONGSHU_SOURCE_MONGO_DATABASE`、`XIAOHONGSHU_SOURCE_MONGO_COLLECTION`，
服务通过现有 `MONGO_URI` 只读查询 URL 与作者标识，不读取来源互动计数。
支持平铺、`rawData` 和日报 `raw` 记录。来源更新后，新 URL 不需要重新导出文件或重启服务。
来源库不可用时会明确失败；部署账号需具有所选集合的读取权限。
小红书异步单条预算默认 300 秒，包含账号限速、恢复链接和分页，其他平台仍使用原有预算。
同步接口仍为 120 秒；小红书批量或长分页使用异步任务。

```bash
conda run -n MyAgent uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8200
```

检查：

```bash
curl http://127.0.0.1:8200/api/v1/health
curl 'http://127.0.0.1:8200/api/v1/interactions?url=https%3A%2F%2Fwww.toutiao.com%2Farticle%2F7557632662635840036%2F&media_name=toutiao'
curl 'http://127.0.0.1:8200/api/v1/comments?url=https%3A%2F%2Fwww.toutiao.com%2Farticle%2F7557632662635840036%2F&media_name=toutiao&page=1'
```

## CLI

```bash
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement \
  interactions \
  'https://www.toutiao.com/article/7557632662635840036/' \
  toutiao
```

临时绕过代理池：

```bash
conda run -n MyAgent python -m app.manually_execute_script.fetch_url_engagement \
  comments \
  'https://www.toutiao.com/article/7557632662635840036/' \
  toutiao \
  --page 1 \
  --direct
```

首次需要平台账号时，在带桌面环境的本机建立独立 Profile；可用 `--url` 直接打开目标内容
页完成登录或安全验证：

```bash
conda run -n MyAgent creator-engagement-login xiaohongshu --url '<小红书笔记 URL>'
```

作者内容接口使用同一 MongoDB 中独立的 `author_jobs`、`author_works` 集合，启动时自动建索引并恢复未完成作者任务。保留一个 worker；作者任务共用互动服务代理池及全局采集并发。每个作者 4 个详情 worker，列表在代理有效期内保留会话、接近到期时换 IP 接着游标执行。浏览器复用现有 Camoufox 依赖及浏览器并发预算，所有列表与详情都遵循强制代理配置。接口范围与平台限制见 `docs/AUTHOR_CONTENT_API.md`。

当前没有账号配置，作者流程使用全自动访客 Cookie：设置 `AUTHOR_GUEST_ONLY=true`、`AUTHOR_USE_SAVED_SESSIONS=false`、`DOUYIN_AUTHOR_SESSION_MODE=anonymous`。第一个开关优先于其余账号配置，旧 Cookie 即使留在部署文件中也不会进入作者请求；前文人工建立 Profile 的流程不适用于此作者采集模式。检查 `/api/v1/health` 的 `author_pipeline_version=7`、`author_guest_only=true`、`author_use_saved_sessions=false`。作者任务失败后自动等待重试；取消测试任务时使用作者任务 cancel 接口，避免遗留运行任务继续消耗代理。
