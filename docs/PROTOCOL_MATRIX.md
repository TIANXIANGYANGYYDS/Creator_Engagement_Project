# 八个平台 16 项协议能力矩阵

最后验证：2026-08-20，环境：`MyAgent` / Python 3.13.12。业务顺序为协议优先，协议被拦或
缺字段时使用按平台隔离的 Camoufox 浏览器 Profile 兜底；不硬编码浏览器 Cookie/签名。

状态含义：

- `可用（部分覆盖）`：纯协议能稳定拿到目标作品的公开字段，但平台可能折叠、限流或只返回当前页。
- `受会话/签名影响`：接口存在，但匿名或无动态状态时不能承诺稳定成功。
- `无法稳定获取`：已定位接口和阻断条件，但不能在无浏览器、无登录态的服务端稳定复现。

| 平台 | 请求 | 当前状态 | 当前接口/证据 | 不能保证的部分 |
|---|---|---|---|---|
| 抖音 | 互动量 | 浏览器已验证可获取 | `/aweme/v1/web/aweme/detail/`；页面运行时生成 `a_bogus`、`msToken`、`UIFID` 等状态 | 统计依赖页面会话；无浏览器的纯协议仍可能 HTTP 200 空包 |
| 抖音 | 评论 | 浏览器按响应判定 | `/aweme/v1/web/comment/list/`；浏览器请求带动态设备风控状态 | 当前样例无会话时 HTTP 200 空包，结果明确说明评论未返回 |
| 头条 | 互动量 | 受 SSR/挑战影响 | 文章 SSR 中的 `itemCounter`/`likeData`；无 `_signature` 的评论接口可用 | 直接协议首包可能是 JSVM 挑战，统计字段可能为空 |
| 头条 | 评论 | 可用（部分覆盖） | `/article/v4/tab_comments/`，参数 `aid/app_name/offset/count/group_id/item_id` | 只能说明指定公开页，不保证评论全集 |
| 公众号 | 互动量 | 浏览器尝试，受文章会话影响 | `/mp/getappmsgext`、文章 SSR | 需要 `__biz/mid/idx/sn/chksm/pass_ticket/appmsg_token` 等文章会话参数 |
| 公众号 | 评论 | 浏览器尝试，受文章会话影响 | `/mp/appmsg_comment?action=getcomment` | `ret=-3, errmsg=no session` 时明确 blocked |
| 小红书 | 互动量 | 可用（部分覆盖） | 详情页 SSR `noteDetailMap.interactInfo` | 部分旧/失效 URL 的 SSR 统计为空，需有效笔记页 |
| 小红书 | 评论 | 浏览器尝试，受登录/签名影响 | `edith.xiaohongshu.com/api/sns/web/v2/comment/page` | 协议签名被 406 时转浏览器；页面仍可能要求登录 |
| 好看 | 互动量 | 可用（字段不完整） | `/haokan/ui-web/v2/comment/get` 可取 `comment_count` | 点赞、播放、分享等详情接口尚未确认稳定参数 |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 浏览器尝试，先校验目标 ID | `visionShortVideoReco`、`visionVideoDetail` | 匿名协议可能返回无关推荐流；浏览器必须匹配目标 `photo.id` |
| 快手 | 评论 | 浏览器尝试，受验证码影响 | `visionCommentList` | `Need captcha` 时明确 blocked，依赖短期 `kww/kwfv1/kwssectoken` |
| B 站 | 互动量 | 可用（部分覆盖） | `/x/web-interface/view` | 返回当前公开计数，平台变化或限流仍可能影响结果 |
| B 站 | 评论 | 可用（部分覆盖） | `/x/v2/reply/wbi/main`；WBI 密钥从 `/x/web-interface/nav` 动态提取并签名 | WBI 是游标接口，项目将公开 `page` 转换为游标遍历；只保证当前页 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 可用（部分覆盖） | `m.weibo.cn/comments/hotflow` | 热门/访客流可能折叠或截断，不能保证评论全集 |

## 结论

当前纯协议稳定范围仍是：B 站 2 项、微博 2 项、头条评论、好看评论；启用浏览器兜底后，抖音互动量已验证可通过动态页面会话获取，头条/小红书/公众号/快手进入页面后按实际响应继续尝试。

仍不应硬编码或伪造的内容是：公众号文章会话参数、小红书动态签名、快手 `kww`/验证码状态和抖音临时 Cookie。遇到登录、验证码或空响应时，接口返回 `blocked`/`unsupported` 与原因。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境仍可使用 Stock 项目同源的 51 代理池，但应把代理失败和平台返回分开记录。

## 公开资料交叉验证

联网检索到的小红书评论工具 `mashukui/xhs_search_comment_tool` 明确要求用户手工填写自己的 Cookie，且不公开签名源码；`shuicici/xiaohongshu-scraper` 使用 Apify/代理并提示部分数据需要认证。抖音公开项目 `intAV/Douyin_live_like` 同样要求手工复制 Cookie。它们能证明“带用户会话的协议采集可行”，但不能提供本项目要求的匿名、无浏览器、可长期部署的签名实现，因此没有把这些项目的一次性 Cookie 方案复制进业务代码。

- <https://github.com/mashukui/xhs_search_comment_tool>
- <https://github.com/shuicici/xiaohongshu-scraper>
- <https://github.com/intAV/Douyin_live_like>
