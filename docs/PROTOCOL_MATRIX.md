# 八个平台 16 项协议能力矩阵

最后验证：2026-08-23，环境：`MyAgent` / Python 3.13.12。业务顺序为协议优先、浏览器
持久化会话接管，不接入付费数据供应商。快手企业协议链路最新全量为互动/评论均
398/398 有数据；小红书互动已修正为匿名 SSR 100/100，低频评论为 20/20。公众号
任意文章互动/评论及小红书无会话深分页不存在
已验证的免费匿名协议，不硬编码浏览器 Cookie/签名。

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
| 公众号 | 互动量 | 无会话无法稳定获取 | 匿名 SSR（含 V2/零值字段）、`/mp/getappmsgext`；官方统计 API 需要自有公众号授权 | 匿名页通常不下发计数；批量访问还可能重定向 `wappoc_appmsgcaptcha`，现已明确标记 `blocked` |
| 公众号 | 评论 | 页面预载首屏可直接取 | `preload_comment_list`、`cgiDataNew.show_comment`、`/mp/appmsg_comment` | 页面预载精选评论免会话；作者关闭返回完整空页；开启但无文章会话仍返回 `ret=-3 no session` |
| 小红书 | 互动量 | 匿名纯协议可用（100/100） | 当前有效 `xsec_token` + `xsec_source=pc_feed` 直访笔记 SSR；自动补来源参数，不带评论 Cookie | 100 条新鲜 URL 各 1 GET，94 条返回赞/藏/评/分享四项、6 条返回页面公开的三项；过期或缺 token 不能恢复任意旧 URL |
| 小红书 | 评论 | 低频首屏可用（20/20） | 会话 + `xhshow` 请求 `/api/sns/web/v2/comment/page` | 本轮 4 秒启动间隔返回 200 行；需要完整 `xsec_token`，深分页和长期持续性未证明 |
| 好看 | 互动量 | 纯协议可用（字段不完整） | 首页匿名 Cookie → 目标页 SSR；精确播放、点赞、评论数 | 收藏、分享没有公开数字时保持 `null` |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 企业协议可用（398/398） | `visionVideoDetail` + SSR Apollo + REST/GraphQL 评论总数；语义失败换 IP | 364 条拿齐播放/点赞/评论；34 条详情不可用，仅返回评论数，不伪造旧点赞 |
| 快手 | 评论 | 企业协议可用（398/398） | `/rest/v/photo/comment/list`，验证码时回退 `commentListQuery`，按 `pcursor` 分页 | 首页共 854 行；只返回一级评论，子回复数量保留但正文需独立 sublist 语义 |
| B 站 | 互动量 | 视频和专栏匿名纯协议可用 | 视频 `/x/web-interface/view`；`cv` `/x/article/viewinfo`；Opus `__INITIAL_STATE__.module_stat` | Opus 页面未公开阅读数时 `views=null`，其余公开赞/评/转/藏/币照实返回；直播不在范围内 |
| B 站 | 评论 | 视频和专栏匿名分页可用 | WBI `/x/v2/reply/wbi/main`：视频 `type=1`、专栏 `type=12`，`mode=2` + `next_offset` | 只返回一级公开评论；WBI 受限时旧接口回退可能只给少量当前可见评论 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 前两页匿名可用（部分覆盖） | 首屏 `comments/hotflow`；登录跳转时降级 `api/comments/show?page=N` | 两个接口排序和总数口径不同；本轮第 2 页成功，第 3 页仍可能 `ok=-100` |

## 结论

当前无需平台账号即可验证：抖音 2 项、B 站 2 项、微博 2 项、头条评论、好看互动量/评论、
快手互动量/一级评论，快手企业全量两项均达到 398/398 有数据，但互动中 34 条只剩评论数。
小红书互动量本轮新鲜 URL 100/100，评论低频 20/20；两者分别使用匿名 SSR 与带会话签名
评论端点，不能合并为一次上游请求；
小红书后续评论页和公众号任意文章互动/评论没有接入付费接口。公众号接口仍区分作者关闭
评论与原生会话 `no session`。

仍不应硬编码或伪造：公众号文章会话参数、小红书动态签名、快手短期游客验证状态和抖音
临时 Cookie。抖音签名和第一方访客标识现已在运行时生成；其他游客状态由浏览器生成。遇到
验证码或空响应时，接口返回 `blocked/unsupported` 与原因。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境仍可使用 Stock 项目同源的 51 代理池，但应把代理失败和平台返回分开记录。

需要账号的路径统一通过 `creator-engagement-login <platform> [--url <同平台内容 URL>]`
在本机建立 Profile。运行时协议客户端和浏览器模拟会复用该平台状态，不要求把密码或
Cookie 交给服务端配置。

完整测试命令、16 条真实请求结果和分页边界见 [`TEST_REPORT.md`](TEST_REPORT.md)。

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
  `xhshow 0.2.0` 双格式签名仍只用于带 `web_session` 的评论分页，不虚构匿名深分页。
- 公众号匿名 `/mp/getappmsgext` 可能返回 `ret=0` 但不含统计，`/mp/appmsg_comment`
  无文章会话返回 `ret=-3`；只有 HTML 已预载的精选评论可直接解析。批量全参数文章实测还会
  进入 `/mp/wappoc_appmsgcaptcha`，现已作为风控而非空数据返回。
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
- <https://greasyfork.org/scripts/535482-wechat-plus/code>
- <https://developers.weixin.qq.com/doc/offiaccount/Analytics/Graphic_Analysis_Data_Interface.html>
- <https://www.bilibili.com/read/cv34832696/>
- <https://www.bilibili.com/opus/907932915033178114>
- <https://github.com/intAV/Douyin_live_like>
- <https://github.com/tamnd/douyin-cli>
- <https://github.com/Johnserf-Seed/f2/blob/main/f2/utils/abogus.py>
- <https://github.com/runningZ1/short_video_py/tree/main/api/douyin%20new/cloudflare%20workers>
