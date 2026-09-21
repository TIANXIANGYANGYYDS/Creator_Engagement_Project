抖音作者作品采集调研，核验日期：2026-09-19。

建议优先参考 Evil0ctal/Douyin_TikTok_Download_API 的 v5 会话与签名流程、DouK-Downloader 的用户搜索和主页分页，并用 MediaCrawler 最近的修复交叉核对 Cookie 参数来源。当前工程最需要验证的是完整请求签名、会话一致性和抖音号解析；单纯扩大代理池无法补齐这些请求字段。

本次完成联网检索、上游当前源码与提交历史检查、本地实现对照。未运行上游采集程序，未修改或部署生产采集代码。下面明确区分已确认的代码差异、上游作者的测试记录和待实测的工程判断。

**值得参考的项目与版本**

| 项目 | 核验版本 | 借鉴重点 | 实际限制 |
| --- | --- | --- | --- |
| [Douyin_TikTok_Download_API v5](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/tree/9fa3e5406694c80ec0a97399f410948f130d0f9e) | `9fa3e54`；主分支最后提交 2026-09-15 | 浏览器建立游客会话、纯 Python 签名与浏览器兜底、身份与代理绑定、错误分类 | 新版接口以 sec_user_id 为主；不能直接替代我们对公开抖音号的解析。上游实测不是本环境验收。 |
| [DouK-Downloader](https://github.com/JoeanAmier/TikTokDownloader/tree/473c90ff70c663cfb69310fff2b8d5192f200661) | `473c90f`；签名参数文件最近修改 2026-09-12 | 用户搜索、搜索游标、作者作品分页、额外 WebSign 参数 | 新签名代码参考同一个 Evil0ctal 上游，因此两者不是独立验证证据。 |
| [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler/tree/8ecfa31de22c5cbc890e5b0b520b450f24ec4b21) | `8ecfa31`；抖音详情修复 2026-09-18 | 浏览器 Cookie/localStorage 会话同步、详情参数来源 | 最近修复针对 detail；不能推定它也验证了我们需要的公开抖音号搜索与完整主页历史。 |
| [F2](https://github.com/Johnserf-Seed/f2/tree/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3) | `7dab3e2`；本次检查的 crawler.py 最后修改 2025-03-06 | 数据结构、分页与历史实现参考 | 本工程签名已经参考 F2；仍有 2026 年的截断和 403 问题记录。 |
| [douyin-cli](https://github.com/Yht20927/douyin-cli/tree/04f0a7695a5db48d9a6d3a89d720697570c4a191) | `04f0a76`；user 命令加入于 2026-07-06 | 从现有浏览器页面发送请求，作为功能对照 | user 命令接受 sec_user_id/主页 URL，不能直接解析抖音号；其展示数据还会截短文案。 |

版本、源码和提交历史快照保存在 `.local/analysis/douyin-research/`。表中的主分支更新时间不等同于每个采集功能最近通过测试的时间。

**最直接的发现：作者作品接口的签名链条有新增参考实现**

Evil0ctal v5 的 [signer.py](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/9fa3e5406694c80ec0a97399f410948f130d0f9e/src/dtk/signing/native/signer.py) 在 a_bogus 之外，从同一会话补充访客字段及 WebSign。DouK 的 [douyin_params.py](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/src/encrypt/douyin_params.py) 明确列出 `/aweme/v1/web/aweme/post/` 和详情接口为额外签名适用路径。

本地 `app/crawlers/platforms/douyin.py:signed_params` 只追加 a_bogus；`app/crawlers/authors/douyin.py` 的协议路径没有添加同源 uifid、verifyFp/fp 和 x-secsdk-web-signature。全工程检索也未找到这些字段的实现。这是确认的代码差异。它是否是本环境某次失败的直接原因，仍须比较同一出口、同一作者、同一游标下的实际响应。

更近的独立证据是 MediaCrawler 的 [2026-09-18 提交 cf513e7](https://github.com/NanmiCoder/MediaCrawler/commit/cf513e70a13a20105571987ce4c56e03e9bf9be3)：它为抖音详情补充 uifid、verifyFp、fp，强调取值来自同一浏览器 Cookie。提交说明报告了修复前的参数缺失错误及修复后的详情成功。该证据支持“会话参数缺失值得优先排查”，但验证范围限于上游的详情请求。

不能仅凭随机 msToken 就认定失败原因。v5 仍允许抖音游客在没有 Cookie token 时使用生成值；源码对不同 token 组合的成功记录属于上游测试。我们的改进应优先读取真实会话已有 token，并记录实际请求差异，不把“改成真实 token”作为唯一修复。

**浏览器兜底也需要可验证的就绪条件**

当前 `app/crawlers/authors/browser.py` 在导航后固定等待 1.5 秒，再尝试捕获响应或执行 fetch；没有检查网页 SDK 是否已接管请求、是否产生有效签名。调用 fetch 本身无法证明签名已生效。

v5 的 [浏览器后端](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/9fa3e5406694c80ec0a97399f410948f130d0f9e/docker/browser_rpc/backends/cloak.py) 会检查 SDK 是否实际改写了请求。它还记录了网页 SDK 缓存访客字段的行为，因此初始化页面前应装入对应 Cookie，不能在旧页面里随意更换另一套会话后继续签名。

适合本工程的改法是增加 SDK 就绪检查和真实请求参数观测。代理切换后为新出口建立并校验对应会话，继续已保存的作者游标；具体是否可以迁移旧会话，应由出口切换实验决定。

**抖音号解析必须单独验收**

用户文件里的 author_id 包含数字账号和字母账号，不能把每个纯数字值都当成内部 UID。目前 `identity.py` 对纯数字输入先尝试 profile/other 的 user_id，再搜索；参考项目的 profile 请求主要使用 sec_user_id。前一个尝试应保留为待证实路径，不能据此承诺数字账号解析可用。

DouK 的 [search.py](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/src/interface/search.py) 实现了用户搜索，使用 discover/search、aweme_user_web，并处理搜索来源、游标和 search_id；[account.py](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/src/interface/account.py) 随后以 sec_user_id 和 max_cursor 分页。现有工程搜索路径方向相符，但请求上下文更少，且账号映射只保存在单个 crawler 对象，换会话后会丢失。

建议持久化 `输入 author_id → 平台 uid/sec_uid/unique_id/short_id` 的已核验映射，保存核验时间。首次解析需要精确账号证据；后续作品以核验过的 sec_uid 检查归属，不能仅按昵称匹配。公开抖音号可能改变，缓存也需要失效和重新核验机制。

**代理与会话应一起调度**

现有列表复用租约、详情并发租约、检查点和续跑设计可以保留。新增的重点是：每个可用采集单元同时持有代理、Cookie、浏览器信息和签名上下文；一个单元成功初始化后，在其有效期内连续翻页。

参考 v5 的 [身份数据结构](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/9fa3e5406694c80ec0a97399f410948f130d0f9e/src/dtk/identity/pool.py) 和 [响应分类](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/9fa3e5406694c80ec0a97399f410948f130d0f9e/src/dtk/transport/classify.py)，我们应把网络失败、签名/验证失败、内容不可见、正常分页结束分开记录。删除的作品不应导致淘汰健康代理；代理连接超时也不应被报成作者不存在。

本地 AuthorService 当前对作者采集强制匿名，且只为小红书转发明确启用的 Cookie。若实验证明抖音某些路径需要登录会话，需要单独增加明确配置的抖音会话模式；仅修改现有通用 Cookie 配置不会自动改变作者接口的行为。

**建议实施与验收顺序**

1. 先用一个已知 sec_uid 分离验证作品列表，排除抖音号解析干扰。对比当前请求、补齐会话参数的请求、浏览器原生请求，记录成功率和脱敏错误原因。
2. 加入完整签名与浏览器就绪检查，确认同一个作者可以取得连续至少 5 页，或平台明确返回末页。
3. 用真实文件抽取数字抖音号、字母抖音号及已有内部标识，分别检验解析；精确账号匹配与作品作者归属都必须通过。
4. 对至少 20 位真实作者重复测试，覆盖至少 3 次代理租约切换，检验继续原游标、去重、失败后续跑。明确记录样本和结果，不把首屏成功推广为全量稳定。
5. 结果报告区分账号解析成功率、首屏成功率、连续分页成功率、换 IP 后恢复成功率、公开内容字段完整率。只有这些指标验证后，才评价是否满足稳定采集需求。

当前没有任何一个参考项目可据 README 就保证任意作者的完整历史。F2 的 [#435](https://github.com/Johnserf-Seed/f2/issues/435) 报告主页只处理约 20 条后停止；[#443](https://github.com/Johnserf-Seed/f2/issues/443) 报告点赞列表两页后 403。后者不是作品列表，不能混作同一接口的故障证据。这些问题说明验收必须包含深分页和明确结束条件。

建议先在现有 FastAPI/MongoDB 作者任务中适配经过验证的签名和会话组件。上游源码引用与许可文件一同核对；Evil0ctal/F2 为 Apache-2.0，DouK 为 GPL-3.0，MediaCrawler 当前采用自定义非商业学习许可，适用的复用方式不同。
