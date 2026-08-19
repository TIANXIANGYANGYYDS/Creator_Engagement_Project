# 八个平台 16 项协议能力矩阵

最后验证：2026-08-20，环境：`MyAgent` / Python 3.13.12。浏览器只用于定位接口和比对请求，业务实现不依赖 Playwright/Camoufox。

状态含义：

- `可用（部分覆盖）`：纯协议能稳定拿到目标作品的公开字段，但平台可能折叠、限流或只返回当前页。
- `受会话/签名影响`：接口存在，但匿名或无动态状态时不能承诺稳定成功。
- `无法稳定获取`：已定位接口和阻断条件，但不能在无浏览器、无登录态的服务端稳定复现。

| 平台 | 请求 | 当前状态 | 当前接口/证据 | 不能保证的部分 |
|---|---|---|---|---|
| 抖音 | 互动量 | 受会话/签名影响 | `/aweme/v1/web/aweme/detail/`；浏览器请求带 `a_bogus`、`msToken`、`UIFID`、`bd-ticket-guard-*` | 匿名请求可能 HTTP 200 空包；需要调用方自己的有效 Cookie，不能写死临时值 |
| 抖音 | 评论 | 无法稳定获取 | `/aweme/v1/web/comment/list/`；浏览器请求带 `a_bogus`、`msToken` 和设备风控状态 | 当前纯协议会话返回空包，不能把空包当成无评论 |
| 头条 | 互动量 | 受 SSR/挑战影响 | 文章 SSR 中的 `itemCounter`/`likeData`；无 `_signature` 的评论接口可用 | 直接协议首包可能是 JSVM 挑战，统计字段可能为空 |
| 头条 | 评论 | 可用（部分覆盖） | `/article/v4/tab_comments/`，参数 `aid/app_name/offset/count/group_id/item_id` | 只能说明指定公开页，不保证评论全集 |
| 公众号 | 互动量 | 无法稳定获取 | 实测 `/mp/getappmsgext` 匿名请求 `base_resp.ret=0`，但响应不下发阅读/点赞统计 | 需要 `__biz/mid/idx/sn/chksm/pass_ticket/appmsg_token` 等文章会话参数 |
| 公众号 | 评论 | 无法稳定获取 | 实测 `/mp/appmsg_comment?action=getcomment` 返回 `ret=-3, errmsg=no session` | 文章会话或登录状态必需 |
| 小红书 | 互动量 | 可用（部分覆盖） | 详情页 SSR `noteDetailMap.interactInfo` | 部分旧/失效 URL 的 SSR 统计为空，需有效笔记页 |
| 小红书 | 评论 | 无法稳定获取 | `edith.xiaohongshu.com/api/sns/web/v2/comment/page` | 每次需要动态 `x-s/x-t/x-s-common`；当前纯协议签名被 406 拒绝 |
| 好看 | 互动量 | 可用（字段不完整） | `/haokan/ui-web/v2/comment/get` 可取 `comment_count` | 点赞、播放、分享等详情接口尚未确认稳定参数 |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 无法稳定获取 | `visionShortVideoReco`；网页端依赖 `webWeapon` | 匿名接口可能返回无关推荐流，不能凭 HTTP 200 认定是目标作品 |
| 快手 | 评论 | 无法稳定获取 | `visionCommentList` | 匿名请求稳定返回 `Need captcha`，依赖短期 `kww/kwfv1/kwssectoken` |
| B 站 | 互动量 | 可用（部分覆盖） | `/x/web-interface/view` | 返回当前公开计数，平台变化或限流仍可能影响结果 |
| B 站 | 评论 | 可用（部分覆盖） | `/x/v2/reply/wbi/main`；WBI 密钥从 `/x/web-interface/nav` 动态提取并签名 | WBI 是游标接口，项目将公开 `page` 转换为游标遍历；只保证当前页 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 可用（部分覆盖） | `m.weibo.cn/comments/hotflow` | 热门/访客流可能折叠或截断，不能保证评论全集 |

## 结论

当前可以进入稳定服务范围的请求是：B 站 2 项、微博 2 项、头条评论、好看评论；头条互动量和小红书互动量属于“能拿到就返回、拿不到明确空字段”的部分覆盖。抖音互动量只能在调用方提供自己的会话 Cookie 时尝试，抖音评论不应标记为成功。

当前不应继续硬编码或伪造的请求是：公众号互动量/评论、小红书评论、快手互动量/评论、抖音评论。它们的关键阻断分别是文章会话参数、动态签名环境、验证码和设备风控状态。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境仍可使用 Stock 项目同源的 51 代理池，但应把代理失败和平台返回分开记录。

## 公开资料交叉验证

联网检索到的小红书评论工具 `mashukui/xhs_search_comment_tool` 明确要求用户手工填写自己的 Cookie，且不公开签名源码；`shuicici/xiaohongshu-scraper` 使用 Apify/代理并提示部分数据需要认证。抖音公开项目 `intAV/Douyin_live_like` 同样要求手工复制 Cookie。它们能证明“带用户会话的协议采集可行”，但不能提供本项目要求的匿名、无浏览器、可长期部署的签名实现，因此没有把这些项目的一次性 Cookie 方案复制进业务代码。

- <https://github.com/mashukui/xhs_search_comment_tool>
- <https://github.com/shuicici/xiaohongshu-scraper>
- <https://github.com/intAV/Douyin_live_like>
