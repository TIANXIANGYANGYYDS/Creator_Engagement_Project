# 八个平台 16 项协议能力矩阵

最后验证：2026-08-22，环境：`MyAgent` / Python 3.13.12。业务顺序为协议优先、浏览器
持久化会话接管，不接入付费数据供应商。快手企业协议链路最新全量为互动/评论均
398/398 有数据；小红书低频评论最新 20/20，但互动在协议三试及浏览器后仍 0/20，不能承诺
当前匿名稳定性。公众号
任意文章互动/评论及小红书深分页不存在
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
| 公众号 | 互动量 | 无会话无法稳定获取 | 匿名 SSR、`/mp/getappmsgext`；官方统计 API 需要自有公众号授权 | 匿名页通常不下发计数，不能仅凭文章 URL 获取任意账号数据 |
| 公众号 | 评论 | 页面预载首屏可直接取 | `preload_comment_list`、`cgiDataNew.show_comment`、`/mp/appmsg_comment` | 只有页面实际预载的精选评论免会话；开启评论但无预载/文章会话时仍返回 `ret=-3 no session` |
| 小红书 | 互动量 | 当前受阻（0/20） | Cookie + `xsec_token` 签名 feed、SSR、4/8 秒重试和浏览器均已执行 | 60 次详情尝试均 HTTP 461；代理+浏览器单测仍是验证码，不能报价为可用 |
| 小红书 | 评论 | 低频首屏可用（20/20） | 会话 + `xhshow` 请求 `/api/sns/web/v2/comment/page` | 本轮 4 秒启动间隔返回 200 行；需要完整 `xsec_token`，深分页和长期持续性未证明 |
| 好看 | 互动量 | 纯协议可用（字段不完整） | 首页匿名 Cookie → 目标页 SSR；精确播放、点赞、评论数 | 收藏、分享没有公开数字时保持 `null` |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 企业协议可用（398/398） | `visionVideoDetail` + SSR Apollo + REST/GraphQL 评论总数；语义失败换 IP | 364 条拿齐播放/点赞/评论；34 条详情不可用，仅返回评论数，不伪造旧点赞 |
| 快手 | 评论 | 企业协议可用（398/398） | `/rest/v/photo/comment/list`，验证码时回退 `commentListQuery`，按 `pcursor` 分页 | 首页共 854 行；只返回一级评论，子回复数量保留但正文需独立 sublist 语义 |
| B 站 | 互动量 | 可用（部分覆盖） | 视频 `/x/web-interface/view`；直播 `Room/get_info` | 直播只返回当前在线、关注和开播状态，不等同于累计互动 |
| B 站 | 评论 | 可用（部分覆盖） | 视频 `/x/v2/reply/wbi/main`；直播 `dM/gethistory` | 视频支持游标分页；直播只提供最近弹幕窗口，不能回溯历史全集 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 前两页匿名可用（部分覆盖） | 首屏 `comments/hotflow`；登录跳转时降级 `api/comments/show?page=N` | 两个接口排序和总数口径不同；本轮第 2 页成功，第 3 页仍可能 `ok=-100` |

## 结论

当前无需平台账号即可验证：抖音 2 项、B 站 2 项、微博 2 项、头条评论、好看互动量/评论、
快手互动量/一级评论，快手企业全量两项均达到 398/398 有数据，但互动中 34 条只剩评论数。
小红书评论本轮低频 20/20，互动量 0/20，不能把历史短时成功冒充当前稳定能力；
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
- 小红书首页可纯协议生成 `a1/webId` 并取得新鲜 `note_id+xsec_token`，但匿名状态下
  `xhshow 0.2.0` 的 `XYS_` 与 `XYW_` 均返回 HTTP 406；因此只把双格式重试用于调用方
  自己的会话，不虚构匿名深分页。
- 公众号匿名 `/mp/getappmsgext` 可能返回 `ret=0` 但不含统计，`/mp/appmsg_comment`
  无文章会话返回 `ret=-3`；只有 HTML 已预载的精选评论可直接解析。
- 抖音按公开实现交叉还原后，用当前 UA、请求参数顺序、第一方 `ttwid` 和本地 `a_bogus`
  完成真实验收；详情和评论均在同一匿名会话返回有效数据，浏览器降为备用路径。
- 好看视频无需复制第三方算法：同一纯协议会话先访问首页，再访问目标页即可得到目标
  SSR，且可用 `og:url` 校验没有误读推荐视频。

- <https://github.com/mashukui/xhs_search_comment_tool>
- <https://github.com/shuicici/xiaohongshu-scraper>
- <https://github.com/openweb-org/openweb/blob/main/src/sites/xiaohongshu/adapters/xiaohongshu-web.ts>
- <https://github.com/Cloxl/xhshow/issues/104>
- <https://github.com/bidabrain/weiboX/blob/main/app/src/main/java/com/weibox/app/data/api/WeiboApi.kt>
- <https://github.com/tamnd/weibo-cli/blob/main/weibo/api.go>
- <https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/kuaishou/client.py>
- <https://github.com/Moore-developers/moore-wechat-article-downloader>
- <https://greasyfork.org/scripts/535482-wechat-plus/code>
- <https://developers.weixin.qq.com/doc/offiaccount/Analytics/Graphic_Analysis_Data_Interface.html>
- <https://github.com/intAV/Douyin_live_like>
- <https://github.com/tamnd/douyin-cli>
- <https://github.com/Johnserf-Seed/f2/blob/main/f2/utils/abogus.py>
- <https://github.com/runningZ1/short_video_py/tree/main/api/douyin%20new/cloudflare%20workers>
