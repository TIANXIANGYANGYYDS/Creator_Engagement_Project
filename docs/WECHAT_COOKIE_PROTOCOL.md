# 微信公众号 Cookie-only 协议流程

最后验证：2026-08-24。该流程只使用用户本机配置的 Cookie、HTTP 请求和现有 IP 池，
业务运行不启动微信客户端、Playwright 或 Camoufox，也不要求把账号密码交给服务。

## 配置

把完整 Cookie Header 写入 Git 已忽略的 `.local/env/.env`，不要粘贴到聊天、源码、命令行
参数或日志：

```dotenv
WECHAT_ARTICLE_COOKIE="wxuin=...; key=...; pass_ticket=...; appmsg_token=...; wap_sid2=..."
```

外部接口不变：

```text
GET /api/v1/interactions?url=<公众号文章 URL>&media_name=wechat
GET /api/v1/comments?url=<公众号文章 URL>&media_name=wechat&page=1
```

## 字段恢复

代码会从文章 `window.cgiDataNew`、文章 URL 和 Cookie 三处合并参数。Cookie 中
`wxuin` 会映射为 `uin`，`version` 映射为 `clientversion`，百分号编码值只解码一次。
公开实现和当前协议样本共同表明，一份完整文章会话通常需要：

| 能力字段 | 可恢复来源 | 用途 |
|---|---|---|
| `uin` | Cookie `wxuin/uin` 或 URL | 会话身份 |
| `key` | Cookie 或 URL | 短时请求能力；不是固定算法密钥 |
| `pass_ticket` | Cookie 或 URL | 短时票据 |
| `appmsg_token` | Cookie、URL 或文章脚本 | 互动/评论会话票据 |
| `wap_sid2` | Cookie Header | 服务端会话 |
| `__biz/mid/idx/sn/comment_id` | `cgiDataNew` 或 URL | 精确定位目标文章 |

IP 池不能计算或替代缺失的会话字段。弱 Cookie 会在结果原因中只列出缺少的字段名，绝不
输出字段值；`ret=-3/no session` 归类为 `unsupported`，不会用同一失效 Cookie 连续换 3 个
IP。HTTP 验证页、验证码或频控才归类为 `blocked`，企业重试可以为这类失败更换 IP。

## 实际请求流程

互动量：

1. 1 个 GET 获取文章 HTML 和文章常量。
2. HTML 没有计数时，使用同一 Cookie、同一代理租约 POST `/mp/getappmsgext`。
3. 解析阅读、点赞、评论、分享和收藏等响应实际存在的字段。

评论第 N 页：

1. 1 个 GET 获取文章参数和可能预载的精选评论。
2. 有预载首屏时，第 1 页直接返回；否则 GET `/mp/appmsg_comment`。
3. 第 2 页及以后从第 1 页开始跟随 `buffer/continue_flag`，把微信游标转换为外部页码。
4. 只返回平台公开的精选评论。只有接口明确返回 `enabled=0` 才判定文章未开放评论。

每个业务调用内的所有上游请求复用一个代理租约，因此不会出现文章 GET 和评论 GET 各买
一个 IP。互动和评论是两个独立外部接口、也是两个独立微信响应，不能合并为一次上游请求。
51 代理单个新 IP 的采购成本仍是 `84 / 100000 = 0.00084 元`；同一 3 分钟 IP 成功复用时
新增 IP 成本为 0。

## 当前验收边界

- 165 项自动化测试通过，包括 Cookie 字段恢复、URL/HTML 优先级、统计请求、评论 buffer
  分页、Cookie 失效、验证页分类、脱敏和禁止公众号浏览器兜底。
- 无 Cookie 真实文章各执行 5 次：互动 5/5 `unsupported`，P50 1.611 秒；评论 5/5
  `unsupported`，P50 0.896 秒。该结果证明不会伪报空数据，不代表完整 Cookie 失败。
- 当前服务器没有用户提供的文章会话 Cookie，所以还不能填写真实 Cookie 路径的成功率、
  P95 或每成功 URL 成本。配置 Cookie 后可在不接触账号的情况下直接复测。
- Cookie 仍然代表既有会话权限，并不等于匿名。过期 Cookie、普通网页 Cookie或只含
  `wap_sid2` 的 Cookie 无法凭 IP 池补成有效文章会话。

旧的本地微信会话桥保留为可选兼容模块，但本部署只要保持
`WECHAT_SESSION_BRIDGE_URL=""` 即不会启用；公众号协议结果也不会自动进入浏览器兜底。
