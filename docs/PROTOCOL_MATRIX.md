# 八个平台 16 项协议能力矩阵

最后验证：2026-08-20，环境：`MyAgent` / Python 3.13.12。业务顺序为协议优先、浏览器
游客会话兜底，不接入付费数据供应商。快手和小红书首屏不需要用户账号；公众号任意文章
互动/评论及小红书深分页不存在已验证的免费匿名协议，不硬编码浏览器 Cookie/签名。

状态含义：

- `可用（部分覆盖）`：纯协议能稳定拿到目标作品的公开字段，但平台可能折叠、限流或只返回当前页。
- `受会话/签名影响`：接口存在，但匿名或无动态状态时不能承诺稳定成功。
- `无法稳定获取`：已定位接口和阻断条件，但不能在无浏览器、无登录态的服务端稳定复现。

本文件只记录能力和验证结论。每个平台的独立代码入口、互动请求、评论分页及浏览器兜底
顺序见 [`PLATFORM_FLOWS.md`](PLATFORM_FLOWS.md)。

| 平台 | 请求 | 当前状态 | 当前接口/证据 | 不能保证的部分 |
|---|---|---|---|---|
| 抖音 | 互动量 | 协议/浏览器已验证可获取 | `/aweme/v1/web/aweme/detail/`；页面运行时生成 `a_bogus`、`msToken`、`UIFID` 等状态 | 统计依赖页面会话；平台对已有互动作品返回 `play_count=0` 时按“未下发”处理为 `null`，不误报零播放 |
| 抖音 | 评论 | 浏览器按响应判定 | `/aweme/v1/web/comment/list/`；浏览器请求带动态设备风控状态 | 当前样例无会话时 HTTP 200 空包，结果明确说明评论未返回 |
| 头条 | 互动量 | 受 SSR/挑战影响 | 文章 SSR 中的 `itemCounter`/`likeData`；无 `_signature` 的评论接口可用 | 直接协议首包可能是 JSVM 挑战，统计字段可能为空 |
| 头条 | 评论 | 可用（部分覆盖） | `/article/v4/tab_comments/`，参数 `aid/app_name/offset/count/group_id/item_id` | 只能说明指定公开页，不保证评论全集 |
| 公众号 | 互动量 | 无会话无法稳定获取 | 匿名 SSR、`/mp/getappmsgext`；官方统计 API 需要自有公众号授权 | 匿名页通常不下发计数，不能仅凭文章 URL 获取任意账号数据 |
| 公众号 | 评论 | 页面预载首屏可直接取 | `preload_comment_list`、`cgiDataNew.show_comment`、`/mp/appmsg_comment` | 只有页面实际预载的精选评论免会话；开启评论但无预载/文章会话时仍返回 `ret=-3 no session` |
| 小红书 | 互动量 | 可用（部分覆盖） | 游客 SSR；自有会话可用 `xhshow` | URL 必须包含有效笔记 ID；旧链接可能已失效 |
| 小红书 | 评论 | 游客首屏可用 | 游客浏览器截获 `/api/sns/web/v2/comment/page` | 游客 UI 只开放首屏；纯算 `XYS_/XYW_` 匿名直连实测仍返回 HTTP 406 |
| 好看 | 互动量 | 纯协议可用（字段不完整） | 首页匿名 Cookie → 目标页 SSR；精确播放、点赞、评论数 | 收藏、分享没有公开数字时保持 `null` |
| 好看 | 评论 | 可用（部分覆盖） | `/haokan/ui-web/v2/comment/get`，`rn/url_key/pn/child_rn` | 只能说明指定页，不保证评论全集 |
| 快手 | 互动量 | 游客可用（实测） | 目标页游客设备会话 + `POST /graphql` `visionVideoDetail` | 严格校验响应 `photo.id`，不接受推荐流冒充目标 |
| 快手 | 评论 | 游客可用（实测） | 同页会话 `POST /rest/v/photo/comment/list` + `pcursor` | 不需要账号；挑战失败时明确 blocked；只返回一级评论 |
| B 站 | 互动量 | 可用（部分覆盖） | `/x/web-interface/view` | 返回当前公开计数，平台变化或限流仍可能影响结果 |
| B 站 | 评论 | 可用（部分覆盖） | `/x/v2/reply/wbi/main`；WBI 密钥从 `/x/web-interface/nav` 动态提取并签名 | WBI 是游标接口，项目将公开 `page` 转换为游标遍历；只保证当前页 |
| 微博 | 互动量 | 可用（部分覆盖） | `m.weibo.cn/statuses/show?id=...` | 访客态字段受限流和可见性影响 |
| 微博 | 评论 | 前两页匿名可用（部分覆盖） | 首屏 `comments/hotflow`；登录跳转时降级 `api/comments/show?page=N` | 两个接口排序和总数口径不同；本轮第 2 页成功，第 3 页仍可能 `ok=-100` |

## 结论

当前无需平台账号即可验证：B 站 2 项、微博 2 项、头条评论、好看互动量/评论、快手互动量/一级
评论、小红书互动量/首屏评论。小红书后续评论页和公众号任意文章互动/评论没有接入付费
接口，也不会把首屏、空包或聚合站缓存冒充为成功。公众号接口仍区分作者关闭评论与原生
会话 `no session`。

仍不应硬编码或伪造：公众号文章会话参数、小红书动态签名、快手短期游客验证状态和抖音临时 Cookie。浏览器会自行生成游客状态；遇到验证码或空响应时，接口返回 `blocked/unsupported` 与原因。

运行时使用 `--direct` 可排除代理池质量对协议验证的干扰；生产环境仍可使用 Stock 项目同源的 51 代理池，但应把代理失败和平台返回分开记录。

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
- 抖音公开纯算项目仍需要临时 Cookie/设备状态，部分项目也明确说明业务接口会触发
  anti-bot wall；未经过当前真实接口验收的 signer 不替换现有已验证路径。
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
