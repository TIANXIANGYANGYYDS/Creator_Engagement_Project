# 部署说明

项目使用 `MyAgent` Conda 环境，已验证 Python 3.13.12。

## 配置

```bash
cp .env.example .local/env/.env
```

部署时将真实的 `PROXY_51_API_URL`、平台会话 Cookie 等值写入
`.local/env/.env`。该目录已被 Git 忽略，不应提交或打印敏感值。

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
