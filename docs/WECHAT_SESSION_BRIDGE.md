# 微信公众号任意文章：本地微信会话桥

## 结论

免费方案不是“匿名接口”，而是把已登录微信客户端当作短时会话提供者。微信桌面端打开
一篇公众号文章时，本地 mitmproxy 只观察 `mp.weixin.qq.com` 的文章、互动和评论响应，
把 `uin/key/pass_ticket/appmsg_token/Cookie` 交给同机的内存桥。桥随后从同一电脑、同一
网络出口请求文章互动量和公开精选评论。原始凭据不写文件、不进入主 API 服务、不打印日志，
约 25 分钟后自动删除。

同一个 `__biz` 的会话可以在有效期内复用到该公众号的多篇文章，不需要每个 URL 都扫码或
逐篇模拟点击。公众号页面只提供作者选出的精选评论，因此接口的 `coverage` 保持 `partial`，
不能宣称是作者后台里的全部留言。

## 组件和数据流

```text
已登录微信桌面端 / WeChatAppEx
  -> 本机 mitmproxy（只转发微信文章相关响应）
  -> 127.0.0.1:8210 本地会话桥
       - 原始短时凭据：仅内存，按 __biz 保存 25 分钟
       - 互动：凭据化文章页，必要时 /mp/getappmsgext
       - 评论：/mp/appmsg_comment，buffer 游标转外部 page
  -> 主服务（只发送 URL、media_name、page，接收结构化结果）
```

主服务和会话桥在不同机器时，不公开监听桥端口。会话桥仍绑定 `127.0.0.1`，使用 SSH
反向/本地隧道连接；非回环 HTTP 地址会被客户端拒绝，远程直连必须使用 HTTPS。

## MyAgent 安装和启动

在运行微信桌面端的电脑克隆本项目，并使用 `MyAgent`：

```bash
conda run -n MyAgent python -m pip install -e '.[wechat]'
```

生成一个随机 token，只写到本机环境，不提交到 Git：

```bash
export WECHAT_SESSION_BRIDGE_TOKEN="$(openssl rand -hex 32)"
export WECHAT_SESSION_BRIDGE_URL="http://127.0.0.1:8210"
conda run -n MyAgent creator-engagement-wechat-bridge
```

另开终端启动抓包适配器：

```bash
export WECHAT_SESSION_BRIDGE_TOKEN='<与上面同一个本地 token>'
export WECHAT_SESSION_BRIDGE_URL='http://127.0.0.1:8210'
conda run -n MyAgent mitmdump \
  --listen-host 127.0.0.1 --listen-port 23344 \
  --quiet \
  -s app/manually_execute_script/wechat_mitm_addon.py
```

首次运行需按 mitmproxy 官方流程访问 `http://mitm.it` 并信任本机 CA，否则无法看到 HTTPS
响应。macOS 可暂时把系统 HTTP/HTTPS 代理设为 `127.0.0.1:23344`；Windows 微信 4.x
建议用 Proxifier 只把 `WeChatAppEx.exe` 指向该代理，减少其他应用流量和系统影响。完成
后应恢复系统代理。不要给 mitmdump 增加 `-w/--save-stream-file`；`--quiet` 用于避免默认
控制台流量摘要打印带短时查询参数的 URL。

保持微信已登录，重新打开目标公众号的一篇文章并滚动到评论区。检查会话状态：

```bash
curl -H "Authorization: Bearer $WECHAT_SESSION_BRIDGE_TOKEN" \
  http://127.0.0.1:8210/health
```

只会返回 `biz/status/available_fields/expires_in_seconds`，不会返回 Cookie 或 token。

## 接入主服务

主服务 `.local/env/.env` 配置：

```dotenv
WECHAT_SESSION_BRIDGE_URL="http://127.0.0.1:8210"
WECHAT_SESSION_BRIDGE_TOKEN="<与本机会话桥相同的随机 token>"
```

外部接口不变：

```text
GET /api/v1/interactions?url=<公众号文章URL>&media_name=公众号
GET /api/v1/comments?url=<公众号文章URL>&media_name=公众号&page=1
```

互动和评论仍是两个业务接口。微信上游也不是一个请求：互动通常来自文章页或
`getappmsgext`，评论正文来自 `appmsg_comment`。如果微信打开文章时已经产生对应响应，桥会
优先复用内存中的结构化观察值；否则再使用同一短时会话发起协议请求。

## 失败含义

- `waiting_session`：当前 `__biz` 没有完整会话；在微信中打开该公众号任意文章刷新。
- `no_data`：会话有效，但当前响应没有下发互动数字，不能把它当作零。
- `show_comment=0`：作者关闭评论，是完整空结果，不是登录失败。
- `ret=-3/no session`：短时会话失效或微信调整校验，重新打开文章捕获。
- `partial`：微信公开的是精选评论或部分互动字段，数据真实但不是后台全量。

## 已验证与未验证

本项目已用模拟的微信原始响应验证：会话合并/过期、脱敏状态、凭据化互动请求、
`appmsg_comment` 页码转换、主接口优先级、远程明文拒绝和鉴权配置。当前 Linux 服务器没有
用户的微信桌面会话，因此不能伪造“真实账号 100 次成功率”；需要在用户电脑完成一次上述
本地捕获后，才能对真实 URL 做 100 次稳定性与耗时测试。

实现参考了 MIT 许可的
[Moore WeChat Article Downloader](https://github.com/Moore-developers/moore-wechat-article-downloader)
对短时凭据范围和内存 broker 的公开验证思路；本项目按自己的 API 和安全边界独立实现。
该项目 2026-07-30 停止的是公众号后台文章列表核心接口，当前页微信会话/互动快照是另一条
链路，但仍应视为随微信版本变化的兼容方案。
