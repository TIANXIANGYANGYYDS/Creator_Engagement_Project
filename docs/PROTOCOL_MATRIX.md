# 九个平台 18 项协议能力矩阵

最后验证：2026-08-28，环境：`MyAgent` / Python 3.13.12。当前生产不执行人工操作、扫码或
浏览器登录，也不查询其他项目或缓存型数据服务。快手企业协议链路最新全量为互动/评论均
398/398 有数据；小红书互动已修正为匿名 SSR 100/100；视频号公开互动量为 100/100、评论
总数为 100/100。小红书评论允许部署方显式提供专用 Cookie；公众号和视频号评论正文仍因
账号会话要求在严格部署中关闭。不硬编码浏览器 Cookie 或签名。

状态含义：

- `可用（部分覆盖）`：纯协议能稳定拿到目标作品的公开字段，但平台可能折叠、限流或只返回当前页。
- `受会话/签名影响`：接口存在，但匿名或无动态状态时不能承诺稳定成功。
- `无法稳定获取`：已定位接口和阻断条件，但不能在无浏览器、无登录态的服务端稳定复现。

本文件只记录能力和验证结论。每个平台的独立代码入口、互动请求、评论分页及浏览器兜底
顺序见 [`PLATFORM_FLOWS.md`](PLATFORM_FLOWS.md)。

| 平台 | 请求 | 当前状态 | 当前接口/证据 | 不能保证的部分 |
|---|---|---|---|---|
| 抖音 | 互动量 | 匿名纯协议可用（已实测） | 第一方初始化 `ttwid`，随机 `msToken` + 纯 Python `a_bogus` 请求 `/aweme/v1/web/aweme/detail/` | 平台对已有互动作品返回 `play_count=0` 时按“未下发”处理为 `null`，不误报零播放 |
| 抖音 | 评论 | 匿名纯协议分页可用（已实测） | 同一访客/IP 请求 `/aweme/v1/web/comment/list/`；首屏/第 2 页各 20 条、总数 2909 | 只能说明公开页；签名/访客规则变化或风控空包时进入浏览器兜底 |
| 头条 | 互动量 | 受 SSR/挑战影响 | 文章 SSR 中的 `itemCounter`/`likeData`；无 `_signature` 的评论接口可用 | 直接协议首包可能是 JSVM 挑战，统计字段可能为空 |
| 头条 | 评论 | 可用（部分覆盖） | `/article/v4/tab_comments/`，参数 `aid/app_name/offset/count/group_id/item_id` | 只能说明指定公开页，不保证评论全集 |
| 公众号 | 互动量 | Cookie-only 纯协议已实现；待用户 Cookie 实测 | 文章 `cgiDataNew` + Cookie/URL 恢复参数；缺计数时 POST `/mp/getappmsgext`；同一业务调用复用一个 IP 租约 | Cookie 通常需 `uin/key/pass_ticket/appmsg_token/wap_sid2`；当前服务器无有效文章 Cookie，匿名 5/5 明确返回 `unsupported` |
| 公众号 | 评论 | 零账号模式不可用 | 历史 `/mp/appmsg_comment` 路径需要文章会话；匿名实测 `ret=-3` | 不允许人工/真实账号后，没有稳定正文来源 |
| 微信视频号 | 互动量 | 公开分享页匿名可用；客户端 feedID 需要授权侧车 | `sph/eid` 使用 `get_feed_info`；`commonFinderJsApi/feedID` 作为 `encrypted_object_id` 发送给授权侧车 | feedID 不能转换为公开 ID；没有授权侧车时返回 `unsupported` |
| 微信视频号 | 评论 | 公开页零账号只有总数；授权侧车可取正文 | `objectId/objectNonceId` + `finderGetCommentList`，按 `lastBuffer` 分页 | 客户端 feedID 无授权会话时无法读取正文；授权终端成功率待实测 |
| 小红书 | 互动量 | 匿名纯协议可用（100/100） | 当前有效 `xsec_token` + `xsec_source=pc_feed` 直访笔记 SSR；自动补来源参数，不带评论 Cookie | 100 条新鲜 URL 各 1 GET，94 条返回赞/藏/评/分享四项、6 条返回页面公开的三项；过期或缺 token 不能恢复任意旧 URL |
| 小红书 | 评论 | Cookie-only 纯协议已接入 | `XIAOHONGSHU_SESSION_MODE=cookie` + `a1/web_session`；`xhshow` 动态签名 `/api/sns/web/v2/comment/page` 并按 cursor 分页 | 默认关闭；历史低频 20/20 已验证协议，当前部署 Cookie 待注入后复测 |
| 好看 | 互动量 | 纯协议可用（字段不完整） | 首页匿名 Cookie → 目标页 SSR；精确播放、点赞、评论数 | 收藏、分享没有公开数字时保持 `null` |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 企业协议可用（398/398） | `visionVideoDetail` + SSR Apollo + REST/GraphQL 评论总数；语义失败换 IP | 364 条拿齐播放/点赞/评论；34 条详情不可用，仅返回评论数，不伪造旧点赞 |
| 快手 | 评论 | 企业协议可用（398/398） | `/rest/v/photo/comment/list`，验证码时回退 `commentListQuery`，按 `pcursor` 分页 | 首页共 854 行；只返回一级评论，保留回复数量但不展开回复正文 |
| B 站 | 互动量 | 视频和专栏匿名纯协议可用 | 视频 `/x/web-interface/view`；`cv` `/x/article/viewinfo`；Opus `__INITIAL_STATE__.module_stat` | Opus 页面未公开阅读数时 `views=null`，其余公开赞/评/转/藏/币照实返回；直播不在范围内 |
| B 站 | 评论 | 视频和专栏匿名分页可用 | WBI `/x/v2/reply/wbi/main`：视频 `type=1`、专栏 `type=12`，`mode=2` + `next_offset` | 只返回一级公开评论；WBI 受限时旧接口回退可能只给少量当前可见评论 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 前两页匿名可用（部分覆盖） | 首屏 `comments/hotflow`；登录跳转时降级 `api/comments/show?page=N` | 两个接口排序和总数口径不同；本轮第 2 页成功，第 3 页仍可能 `ok=-100` |

## 结论

当前无需平台账号即可验证：抖音 2 项、B 站 2 项、微博 2 项、头条评论、好看互动量/评论、
快手互动量/一级评论，快手企业全量两项均达到 398/398 有数据，但互动中 34 条只剩评论数。
小红书互动量本轮新鲜 URL 100/100，评论低频 20/20；两者分别使用匿名 SSR 与显式 Cookie
签名评论端点，不能合并为一次上游请求。公众号已区分 Cookie-only、自有公众号
官方降级、接口明确关闭评论、匿名隐藏评论区与原生会话 `no session`。视频号与公众号不是一个渠道：
视频号公开互动端点已接入；客户端 URL 的互动量和正文都需要授权微信客户端侧车。

## 默认匿名与小红书显式 Cookie 的评论覆盖

`/comments` 默认顺序获取并合并全部当前可见一级评论；显式传 `page=N` 时只返回第 N 页。
“全公开页”只代表可以翻到平台对当前匿名访客公开的末游标，不含已删、隐藏、审核折叠内容。

| 平台 | 一级正文 | 分页能力 | 当前依据 |
|---|---|---|---|
| 抖音 | 支持 | 全公开页 | 实测 20 条/页；按公开 cursor 继续 |
| 今日头条 | 支持 | 全公开页 | `tab_comments` 按 offset 分页 |
| 好看视频 | 支持 | 全公开页 | `comment/get` 按 `pn` 分页到 `is_over` |
| 快手 | 支持 | 全公开页 | `comment/list` 按 `pcursor`，使用自动游客状态 |
| B 站 | 支持 | 全公开页 | WBI 主评论接口按 `next_offset` 分页 |
| 微博 | 支持 | 可翻多页，不保证到底 | 第 2 页已实测；更深页可能返回 `ok=-100` 登录跳转 |
| 微信公众号 | 不支持 | 不适用 | 无文章会话时 `appmsg_comment` 返回 `ret=-3/no session` |
| 微信视频号 | 严格匿名不支持正文；授权侧车支持 | 授权侧车按不透明游标分页 | 公开 `get_feed_info` 只返回总数；正文来自 `finderGetCommentList` |
| 小红书 | 默认不支持；Cookie 模式支持 | Cookie 模式全会话可见页 | 必须显式配置非空 `a1/web_session` 和 URL `xsec_token`；纯协议 cursor 分页 |

当前没有固定“仅第一页”的一级评论实现。评论对象里的 `replies` 只表示回复数量，服务不会
请求或返回评论下的回复正文。

仍不应硬编码或伪造：公众号文章/视频号客户端会话参数、小红书 Cookie/动态签名、快手短期游客验证状态和抖音
临时 Cookie。小红书 Cookie 只从本地 Secret 配置读取；签名和其他游客状态在运行时生成。遇到
验证码或空响应时，接口返回 `blocked/unsupported` 与原因。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境可配置 51 代理池，但应把代理失败和平台返回分开记录。

历史登录/Profile 工具仅保留兼容，当前严格匿名生产不得调用。账号会话能力必须由部署方显式
启用：小红书使用专用 Cookie，视频号使用隔离的授权 Windows 微信侧车。

完整测试命令、18 条能力结果和分页边界见 [`TEST_REPORT.md`](TEST_REPORT.md)。

## 公开资料交叉验证

联网证据与本地真实响应交叉验证：小红书公开页面会下发首屏评论；快手游客页能在同一
页面上下文请求详情和一级评论；微博存在匿名页码评论接口。自有会话签名使用独立 MIT 包
`xhshow==0.2.0`，不复制 MediaCrawler 的非商业代码。公众号开源归档工具也明确把动态
互动/评论限定为短时微信会话。

本轮对公开实现逐项复现后的结论：

- 微博 `api/comments/show?id&page` 确实可在无账号时读取第 2 页，已作为 `hotflow`
  登录跳转后的纯协议降级；访客 `SUB/SUBP` 即使完成 `incarnate`，仍不能解除更深页限制。
- 小红书推荐页可匿名下发新鲜 `note_id+xsec_token`；笔记详情必须补
  `xsec_source=pc_feed`，且不能错误携带评论会话 Cookie。修正后匿名 SSR 百次为 100/100。
  `xhshow 0.2.0` 双格式签名只用于显式 Cookie 模式的评论分页，不虚构匿名深分页。
- 公众号非官方主路径采用调用方 Cookie + 纯 HTTP：从 Cookie、URL 与 `cgiDataNew` 合并
  `uin/key/pass_ticket/appmsg_token` 等字段；互动和精选评论仍是两个微信上游响应。官方降级
  使用 2026 年当前的 `getarticletotaldetail` 与 `comment/list`，令牌由
  `stable_token` 缓存；它解决自有公众号，不扩大到任意第三方文章。匿名
  `/mp/getappmsgext` 可能返回 `ret=0` 但不含统计，`/mp/appmsg_comment`
  无文章会话返回 `ret=-3`；只有 HTML 已预载的精选评论可直接解析。匿名
  `show_comment=0` 只说明当前页未展示评论区，不能推断作者关闭。批量全参数文章实测还会
  进入 `/mp/wappoc_appmsgcaptcha`，现已作为风控而非空数据返回。
- 视频号公开分享 URL 可匿名 POST `get_feed_info`。三条有效分享 URL 轮换百测，互动计数
  100/100；评论接口也 100/100 取得总数，但匿名正文 0/100。当前 MIT 项目
  `nobiyou/wx_channel` v5.7.7 已公开 `feed/profile`、`feed/search` 和
  `feed/comment/list`：注入脚本在微信页面调用 `finderGetCommentList`，本地 WebSocket/HTTP
  返回 `commentInfo/countInfo/lastBuffer`。客户端 `feedID` 需要本项目的最小 HTTP 透传补丁；
  没有授权微信页面时，客户端 `feedID` 的互动量和评论正文都明确返回不可用。
- 用户提供的 CSDN 视频号文章只展示第三方返回值：字段与
  `finderGetCommentList` 的 `objectId/objectNonceId/lastBuffer` 链路一致，但没有公布
  上游 URL、请求头、登录参数、签名或可运行代码，不能独立复现。另一篇所引
  `YzsCmy/wx_video` 实际是七年前的仿短视频微信小程序，不是视频号采集器。
- 额外复现 `wx_video_sdk` 的视频号助手登录链：匿名 POST
  `/cgi-bin/mmfinderassistant-bin/auth/auth_login_code` 实测返回 HTTP 201、`errCode=0`
  和临时 token，但不下发 Cookie；必须用有视频号权限的微信扫码确认后，
  `auth_login_status` 才会返回账号 Cookie。其 `/comment/comment_list` 以运营后台
  `exportId` 读取扫码账号自有作品评论，不是任意公开视频 URL 的通用评论接口。
- B 站当前专栏实测：`cv34832696` 一次详情 GET 返回 7 类互动字段；WBI 使用 `type=12`
  返回 18 条一级评论、总计数 23。另测 4 个 Opus，5/5 互动有数据、5/5 评论有正文；页面
  直接提供每篇的评论 oid/type，无需登录或浏览器。直播 URL 已从路由和成本口径移除。
- 抖音按公开实现交叉还原后，用当前 UA、请求参数顺序、第一方 `ttwid` 和本地 `a_bogus`
  完成真实验收；详情和评论均在同一匿名会话返回有效数据，浏览器降为备用路径。
- 好看视频无需复制第三方算法：同一纯协议会话先访问首页，再访问目标页即可得到目标
  SSR，且可用 `og:url` 校验没有误读推荐视频。

- <https://github.com/mashukui/xhs_search_comment_tool>
- <https://github.com/tamnd/xiaohongshu-cli>
- <https://github.com/shuicici/xiaohongshu-scraper>
- <https://github.com/openweb-org/openweb/blob/main/src/sites/xiaohongshu/adapters/xiaohongshu-web.ts>
- <https://github.com/Cloxl/xhshow/issues/104>
- <https://github.com/bidabrain/weiboX/blob/main/app/src/main/java/com/weibox/app/data/api/WeiboApi.kt>
- <https://github.com/tamnd/weibo-cli/blob/main/weibo/api.go>
- <https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/kuaishou/client.py>
- <https://github.com/Moore-developers/moore-wechat-article-downloader>
- <https://github.com/wnma3mz/wechat_articles_spider/issues/57>
- <https://github.com/xiguawang/wechat-reader>
- <https://github.com/openatx/uiautomator2/issues/1040>
- <https://www.yaklang.com/en/Yaklab/WeChatAppEx/>
- <https://docs.mitmproxy.org/stable/overview/getting-started/>
- <https://docs.mitmproxy.org/stable/addons/overview/>
- <https://greasyfork.org/scripts/535482-wechat-plus/code>
- <https://developers.weixin.qq.com/doc/offiaccount/Analytics/Graphic_Analysis_Data_Interface.html>
- <https://developers.weixin.qq.com/doc/service/api/wedata/news/api_getarticletotaldetail>
- <https://developers.weixin.qq.com/doc/service/api/leaving/api_listcomment>
- <https://developers.weixin.qq.com/doc/service/api/base/api_getstableaccesstoken.html>
- <https://github.com/fatecannotbealtered/wechat-mp-cli/blob/main/docs/OFFICIAL_ENDPOINT_COVERAGE_zh.md>
- <https://github.com/nobiyou/wx_channel/blob/main/docs/API_QUICK_START.md>
- <https://github.com/nobiyou/wx_channel/blob/main/web/docs/COMMENT_CAPTURE.md>
- <https://github.com/nobiyou/wx_channel/blob/main/internal/assets/inject/api_client.js>
- <https://github.com/hjyl-cheng/wechat-pcspider>
- <https://github.com/ltaoo/wx_channels_download/blob/main/internal/api/sph.go>
- <https://github.com/dsxksss/wx_video_sdk>
- <https://blog.csdn.net/YCHMBb/article/details/145325055>
- <https://blog.csdn.net/weixin_44121163/article/details/139326161>
- <https://gitee.com/yzscmy/wx_video/blob/master/README.md>
- <https://www.bilibili.com/read/cv34832696/>
- <https://www.bilibili.com/opus/907932915033178114>
- <https://github.com/intAV/Douyin_live_like>
- <https://github.com/tamnd/douyin-cli>
- <https://github.com/Johnserf-Seed/f2/blob/main/f2/utils/abogus.py>
- <https://github.com/runningZ1/short_video_py/tree/main/api/douyin%20new/cloudflare%20workers>
