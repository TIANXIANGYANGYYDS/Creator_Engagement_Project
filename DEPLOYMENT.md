# 部署说明

项目使用 `MyAgent` Conda 环境，已验证 Python 3.13.12。

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

生产默认资源上限为 4 个采集、2 个浏览器、2 个代理 IP。低于约 2 GB 可用内存时建议：

```dotenv
BROWSER_MAX_CONCURRENCY=1
COLLECTION_MAX_CONCURRENCY=4
PROXY_POOL_SIZE=2
PROXY_MAX_CONCURRENCY=2
ENGAGEMENT_CACHE_TTL_SECONDS=120
ENGAGEMENT_CACHE_MAX_ENTRIES=1000
ENGAGEMENT_CACHE_MAX_BYTES=67108864
```

Uvicorn 建议只启动 1 个 worker；每增加一个 worker 都会复制浏览器并发槽位、缓存和代理池，
不能靠多 worker 提高本项目吞吐。容量和成本估算见 `docs/COST_AND_CAPACITY.md`。

代理模式由 `PROXY_MODE` 控制：

- `direct`：本机直连；
- `prefer`：有 51 代理配置时使用代理池，否则直连；
- `required`：必须取得代理，获取失败直接报错。

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
