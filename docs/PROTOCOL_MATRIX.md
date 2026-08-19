# 八个平台 16 项协议能力矩阵

最后验证：2026-08-20，环境：`MyAgent` / Python 3.13.12。业务顺序为协议优先；需要账号的
平台通过本机一次性登录保存 storage-state，业务请求随后只使用本地会话和动态协议签名，
不硬编码浏览器 Cookie/签名。

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
| 公众号 | 互动量 | 可用（部分覆盖） | 文章 SSR `appmsgstat`，会话有效时尝试 `/mp/getappmsgext` | 文章会话参数可能只在微信客户端链路出现，匿名页可能没有计数 |
| 公众号 | 评论 | 受文章会话影响 | `/mp/appmsg_comment?action=getcomment` | `show_comment=0` 表示作者关闭评论；`ret=-3, errmsg=no session` 明确 blocked |
| 小红书 | 互动量 | 可用（部分覆盖） | 登录态 + `xhshow` 签名调用 `/api/sns/web/v1/feed`，无会话回退详情 SSR | URL 必须包含有效笔记 ID；旧链接可能已失效 |
| 小红书 | 评论 | 可用（登录态、部分覆盖） | `edith.xiaohongshu.com/api/sns/web/v2/comment/page` + `x-s/x-t/x-s-common` | 需要有效 cookie 和 URL 中的 `xsec_token`，只保证指定页 |
| 好看 | 互动量 | 可用（字段不完整） | `/haokan/ui-web/v2/comment/get` 可取 `comment_count` | 点赞、播放、分享等详情接口尚未确认稳定参数 |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 可用（登录态、部分覆盖） | `POST /graphql` 的 `visionVideoDetail` | 需要登录态；严格校验响应 `photo.id`，不接受推荐流冒充目标 |
| 快手 | 评论 | 可用（登录态、部分覆盖） | `POST /rest/v/photo/comment/list`，失败时回退 `visionCommentList` | `Need captcha` 时明确 blocked；只返回一级评论 |
| B 站 | 互动量 | 可用（部分覆盖） | `/x/web-interface/view` | 返回当前公开计数，平台变化或限流仍可能影响结果 |
| B 站 | 评论 | 可用（部分覆盖） | `/x/v2/reply/wbi/main`；WBI 密钥从 `/x/web-interface/nav` 动态提取并签名 | WBI 是游标接口，项目将公开 `page` 转换为游标遍历；只保证当前页 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 可用（部分覆盖） | `m.weibo.cn/comments/hotflow` | 热门/访客流可能折叠或截断，不能保证评论全集 |

## 结论

当前无需登录即可验证的纯协议范围仍是：B 站 2 项、微博 2 项、头条评论、好看评论；建立本地会话后，快手互动量/一级评论和小红书签名互动量/一级评论进入协议适配器。公众号仍受文章是否开启评论以及微信文章会话类型限制，接口会区分关闭评论和 `no session`。

仍不应硬编码或伪造的内容是：公众号文章会话参数、小红书动态签名、快手 `kww`/验证码状态和抖音临时 Cookie。首次登录使用 `creator-engagement-login <platform>`，后续请求读取 `.local/platform-sessions/<platform>.json`；遇到登录、验证码或空响应时，接口返回 `blocked`/`unsupported` 与原因。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境仍可使用 Stock 项目同源的 51 代理池，但应把代理失败和平台返回分开记录。

## 公开资料交叉验证

联网检索到的小红书评论工具 `mashukui/xhs_search_comment_tool` 明确要求用户手工填写自己的 Cookie；项目实际采用独立 MIT 包 `xhshow==0.2.0` 生成动态签名，不复制 MediaCrawler 的非商业代码。快手 GraphQL 结构参考公开 `kuaishou-comment-scraper` 和 MediaCrawler 文档，只实现本项目需要的详情、一级评论和目标 ID 校验。

- <https://github.com/mashukui/xhs_search_comment_tool>
- <https://github.com/shuicici/xiaohongshu-scraper>
- <https://github.com/intAV/Douyin_live_like>
