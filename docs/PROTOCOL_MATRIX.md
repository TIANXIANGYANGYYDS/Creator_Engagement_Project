# 八个平台 16 项协议能力矩阵

最后验证：2026-08-20，环境：`MyAgent` / Python 3.13.12。业务顺序为协议（含可选供应商）
优先、浏览器游客会话兜底。快手和小红书首屏不需要用户账号；公众号和小红书深分页
可配置 AIDATA API Key（供应商凭据，不是平台账号），不硬编码浏览器 Cookie/签名。

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
| 公众号 | 互动量 | 可用（可选供应商） | 匿名 SSR；AIDATA `/api/v2/data/weixin/mp/article/stats` | 原生匿名页通常不下发计数；完整字段需供应商 API Key |
| 公众号 | 评论 | 可用（可选供应商） | `show_comment`；AIDATA `/api/v2/data/weixin/mp/article/comments` + `buffer` | 作者关闭评论时为完整空结果；原生接口仍会 `no session` |
| 小红书 | 互动量 | 可用（部分覆盖） | 游客 SSR；自有会话 `xhshow`；AIDATA note detail | URL 必须包含有效笔记 ID；旧链接可能已失效 |
| 小红书 | 评论 | 首屏游客可用；深分页可选供应商 | 游客 `/api/sns/web/v2/comment/page`；AIDATA `cursor` | 游客 UI 只开放首屏，项目不会伪造后续页 |
| 好看 | 互动量 | 可用（字段不完整） | `/haokan/ui-web/v2/comment/get` 可取 `comment_count` | 点赞、播放、分享等详情接口尚未确认稳定参数 |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 游客可用（实测） | 目标页游客设备会话 + `POST /graphql` `visionVideoDetail` | 严格校验响应 `photo.id`，不接受推荐流冒充目标 |
| 快手 | 评论 | 游客可用（实测） | 同页会话 `POST /rest/v/photo/comment/list` + `pcursor` | 不需要账号；挑战失败时明确 blocked；只返回一级评论 |
| B 站 | 互动量 | 可用（部分覆盖） | `/x/web-interface/view` | 返回当前公开计数，平台变化或限流仍可能影响结果 |
| B 站 | 评论 | 可用（部分覆盖） | `/x/v2/reply/wbi/main`；WBI 密钥从 `/x/web-interface/nav` 动态提取并签名 | WBI 是游标接口，项目将公开 `page` 转换为游标遍历；只保证当前页 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 可用（部分覆盖） | `m.weibo.cn/comments/hotflow` | 热门/访客流可能折叠或截断，不能保证评论全集 |

## 结论

当前无需平台账号即可验证：B 站 2 项、微博 2 项、头条评论、好看评论、快手互动量/一级
评论、小红书互动量/首屏评论。小红书后续评论页和公众号互动/评论可通过 AIDATA 完成，
只需要供应商 API Key。公众号接口仍区分作者关闭评论与原生会话 `no session`。

仍不应硬编码或伪造：公众号文章会话参数、小红书动态签名、快手短期游客验证状态和抖音临时 Cookie。浏览器会自行生成游客状态；遇到验证码或空响应时，接口返回 `blocked/unsupported` 与原因。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境仍可使用 Stock 项目同源的 51 代理池，但应把代理失败和平台返回分开记录。

## 公开资料交叉验证

联网证据与本地真实浏览器响应交叉验证：小红书公开页面会下发首屏评论；快手游客页能在
同一页面上下文请求详情和一级评论。自有会话签名使用独立 MIT 包 `xhshow==0.2.0`，不复制
MediaCrawler 的非商业代码。AIDATA 仅作为可配置的第三方无平台登录后备。

- <https://github.com/mashukui/xhs_search_comment_tool>
- <https://github.com/shuicici/xiaohongshu-scraper>
- <https://github.com/imoonkey/openweb/blob/main/src/sites/xiaohongshu/adapters/xiaohongshu-web.ts>
- <https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/kuaishou/client.py>
- <https://aidata.vip/zh-cn/api/endpoints/xiaohongshu/note/comments>
- <https://aidata.vip/zh-cn/api/endpoints/weixin/mp/article/stats>
- <https://aidata.vip/zh-cn/api/endpoints/weixin/mp/article/comments>
- <https://github.com/intAV/Douyin_live_like>
