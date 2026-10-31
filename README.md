# Creator Engagement

按内容 URL 采集公开互动量与一级评论。协议请求优先，必要时使用现有 Camoufox 兜底；未公开数据返回 `null`，不把访问失败或空页面伪装成零值。

当前注册 **61 个规范媒体来源**，不同来源可能共享同一协议，也可能只支持部分字段。每一个采集器、字段和评论限制统一维护在 [平台清单](docs/PLATFORM_SUPPORT.md)。历史 20,000 条数据的前 80 名及最新批次长尾缺口见 [覆盖与待办](docs/PLATFORM_BACKLOG.md)，本轮修复、回放和并发实测见 [2026-10-31 验收](docs/PLATFORM_EXPANSION_2026-10-31.md)。

## 接口

```text
POST /api/v1/jobs                         提交大批量任务
GET  /api/v1/jobs/{job_id}                 查询进度
GET  /api/v1/jobs/{job_id}/results         按 cursor 读取结果
POST /api/v1/collect                      同步小批量兼容入口
GET  /api/v1/interactions                 单篇互动量
GET  /api/v1/comments                     一级评论
GET  /api/v1/health                       服务状态与注册平台
```

单篇请求只需 `url` 和 `media_name`。评论不传 `page` 时尝试合并当前可访问的全部一级评论；传 `page=N` 时读取指定页。每页条数由上游决定；回复仅返回数量，不展开正文。任务结果的 `item_id` 用于关联调用方原始数据。

```bash
curl -G 'http://127.0.0.1:8200/api/v1/comments' \
  --data-urlencode 'media_name=选车网' \
  --data-urlencode 'url=https://www.chooseauto.com.cn/news/1040092.shtml' \
  --data-urlencode 'page=1'
```

- [评论接口 IO](docs/COMMENTS_API_IO.md)：可交给调用方的入参、出参和错误示例。
- [完整 API](docs/API.md)、[快速调用](docs/API_QUICKSTART.md)：批量、分页、幂等、结果语义。
- [作者作品 API](docs/AUTHOR_CONTENT_API.md)：与互动/评论不同的业务入口，支持范围独立。
- 运行中的 `/docs`、`/openapi.json` 提供接口定义。参数错误返回 422；上游无法提供可用数据时返回 502；批量逐项保留失败原因。

## 会话和代理

默认 `STRICT_ANONYMOUS_MODE=true`、`PROXY_MODE=required`。普通目标请求复用项目代理池及租约，没有代理时明确失败。微信公众号与视频号的匿名评论能力受限，配置其他渠道的账号不能消除它们的会话要求。

小红书保留已有真实账号和专用会话进程，固定使用本机出口 IP，不使用代理池。调用方仍只传 URL，访问参数恢复在服务内部进行，不要求调用方提交作者信息。已有登录不保证每个裸 URL 都能恢复：缺少可核验线索、作品不可见、参数失效时会明确报错。当前部署为账号单并发、每次上游请求结束后至少间隔 15 秒；本轮没有扩大这部分流量，也没有重新登录或替换账号。

普通平台可以同时执行，但还受全局、平台共享协议、代理和浏览器容量限制。当前部署保留全局 8、代理池 8、单 IP 并发 1、普通发布方并发 1。已验证提高单 IP/平台并发会降低本轮样本成功率，因此没有直接放大线上值。调度改进使慢平台限速等待不会占满整批 worker；同一出版方的多个别名共用上游预算。详见 [执行流程](docs/PLATFORM_FLOWS.md) 与 [成本和并发](docs/COST_AND_CAPACITY.md)。

## 安装、运行和验证

使用 Python 3.11+；当前机器统一使用 `MyAgent` 环境。

```bash
conda run -n MyAgent python -m pip install '.[browser,test]'
mkdir -p .local/env
cp .env.example .local/env/.env
conda run -n MyAgent uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8200
```

已有部署不要覆盖 `.local/env/.env`。配置字段与默认值以 [.env.example](.env.example) 和 [config.py](app/core/config.py) 为准，生产差异及重启步骤见 [部署说明](DEPLOYMENT.md)。

```bash
conda run -n MyAgent python -m pytest -q
conda run -n MyAgent creator-engagement interactions '<内容 URL>' bilibili
conda run -n MyAgent creator-engagement comments '<内容 URL>' 哔哩哔哩 --page 1
```

自动化用例验证字段归属、空值、分页、会话、缓存、调度和持久化；实网采集需单独检查业务结果，HTTP 200 不等于成功。最新测试结果见 [测试报告](docs/TEST_REPORT.md)。诊断脚本关闭外部状态告警，原始响应和含会话材料只放 `.local/analysis/`。

## 代码与文档入口

| 位置 | 职责 |
|---|---|
| `app/api/` | 参数校验、响应格式、任务与健康接口 |
| `app/models/` | API 与内部结果模型 |
| `app/services/engagement_service.py` | 并发、限速、缓存、重复请求合并 |
| `app/services/job_service.py` | 批任务调度、超时、取消/恢复、结果持久化 |
| `app/repositories/` | MongoDB/SQLite 存储 |
| `app/crawlers/engagement.py` | 平台执行、重试、熔断、评论遍历和兜底 |
| `app/crawlers/platforms/` | [逐来源实现清单](docs/PLATFORM_SUPPORT.md) |
| `app/crawlers/http_client.py`、`proxy_provider.py` | HTTP 请求、代理租约、复用与故障处理 |
| `app/crawlers/xiaohongshu_session.py`、`xiaohongshu_url_resolver.py` | 已有账号会话和 URL 内部恢复 |
| `app/manually_execute_script/` | 运维/诊断 CLI，不作为新采集协议的放置目录 |
| `tests/` | 自动化回归 |
| `docs/` | [文档索引](docs/INDEX.md)、当前规范与带日期的历史证据 |

历史报告保留原日期；当前平台范围统一以平台清单和运行中的健康接口为准。
