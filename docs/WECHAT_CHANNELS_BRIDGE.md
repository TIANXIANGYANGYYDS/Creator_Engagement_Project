# 微信视频号评论正文：授权客户端侧车

视频号公开预览接口只下发互动量和评论总数。评论正文位于微信客户端页面内部的
`finderGetCommentList` 会话接口中。项目因此把两类能力拆开：

```text
互动量 -> 主服务器匿名 POST get_feed_info
评论正文 -> Windows 微信客户端 -> wx_channel -> 本项目 HTTP 适配器
```

主服务器不接收微信密码、扫码结果或原始 Cookie；它只把公开分享 URL、页码和每页上限发给
调用方自建的侧车。侧车在已授权微信页面中解析 `objectId/objectNonceId`，调用评论列表并以
`lastBuffer` 翻页。若分享详情没有返回 nonce，适配器会用标题和作者做精确搜索；只有唯一匹配
时才继续，避免把推荐视频评论当成目标结果。

## Windows 侧车

当前兼容 `nobiyou/wx_channel` v5.7.3 的本地 HTTP API。该项目为 MIT 许可，默认代理端口
2025、API 端口 2026。

1. 从项目 Releases 下载并解压 `wx_channel.exe`。
2. 在 PowerShell 中设置随机令牌并启动：

   ```powershell
   $env:WX_CHANNEL_TOKEN = "<随机长令牌>"
   .\wx_channel.exe
   ```

3. 按其说明安装本地证书，启动微信并打开视频号页面。必须等页面内部 API 初始化完成。
4. 检查本地状态：

   ```powershell
   curl.exe -H "X-Local-Auth: <随机长令牌>" `
     "http://127.0.0.1:2026/api/channels/status"
   ```

这一步不会要求把账号或 Cookie 发给采集服务器。没有可用微信页面时，侧车会返回不可用，
主接口将降级为只返回公开评论总数。

## 主服务器配置

推荐用 WireGuard、Tailscale 或 SSH 反向隧道把 Windows 的 2026 端口映射成主服务器的
回环端口，不要把侧车直接暴露到公网。例如隧道映射到服务器 `127.0.0.1:22026` 后：

```text
WECHAT_CHANNELS_BRIDGE_URL="http://127.0.0.1:22026"
WECHAT_CHANNELS_BRIDGE_TOKEN="<与 Windows 相同的随机长令牌>"
```

非回环地址必须使用 HTTPS 并配置 token；客户端会拒绝明文远程 HTTP。配置生效后，外部接口
仍保持不变：

```text
GET /api/v1/interactions?url=<视频号分享 URL>&media_name=wechat_channels
GET /api/v1/comments?url=<视频号分享 URL>&media_name=wechat_channels&page=1
```

评论成功路径直接调用侧车，不会先多请求一次公开预览。侧车失败时才匿名请求
`get_feed_info`，保留准确评论总数和失败原因。主服务器不会为该路径租用 51 代理 IP。

## 返回范围与运维边界

- 当前统一接口只返回一级评论正文、昵称、时间、点赞数和回复数量。
- `next_page` 只在响应含非空 `lastBuffer` 时给出；空游标表示当前可见列表结束。
- 平台删除、折叠、可见性和账号权限会影响结果，因此有下一页时 `coverage=partial`。
- 单个 Windows 微信页面先按 0.5–1 秒请求间隔运行；扩容应增加独立授权终端，不能用主服务
  并发直接压同一客户端。
- 没有任何微信客户端会话时，只有互动量和评论总数能匿名完成；IP 池和网页 Cookie 不能生成
  `finderGetCommentList` 的客户端权限。

上游实现与协议证据：

- <https://github.com/nobiyou/wx_channel>
- <https://github.com/nobiyou/wx_channel/blob/main/docs/API_QUICK_START.md>
- <https://github.com/nobiyou/wx_channel/blob/main/web/docs/COMMENT_CAPTURE.md>
- <https://github.com/nobiyou/wx_channel/blob/main/internal/assets/inject/api_client.js>
