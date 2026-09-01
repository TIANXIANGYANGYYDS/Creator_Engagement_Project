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
JOB_MAX_ITEMS=5000
JOB_RESULT_MAX_BYTES=8388608
```

Mongo 用户只需对 `creator_engagement` 库具有 `readWrite` 权限。首次迁移前先停止新任务并执行：

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
