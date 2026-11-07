# 平台与采集器清单

更新：2026-11-07。当前工作区注册 **86 个规范来源名称**（在 85 个来源上增加风闻），包含共享协议、仅支持部分字段和评论受限的来源。注册数量不能等同全字段/全评论可用。11 月 1 日的前 80 名覆盖快照见 [覆盖与待办](PLATFORM_BACKLOG.md)、[历史验收](PLATFORM_EXPANSION_2026-11-01.md) 和 [联合来源 CSV](PLATFORM_COVERAGE_2026-11-01.csv)。工作区能力不代表运行中的旧发布版本已更新。

调用方继续提供 `url + media_name`；小红书无需额外提交作者或访问参数。服务优先使用已有账号会话按笔记 ID 查询访问参数，无需作者线索；仍受作品可见性、会话状态和平台返回结果限制。

微信公众号和微信视频号按业务要求停用采集。服务入口直接返回失败，不读取旧成功缓存、不等待采集限流、不调用协议、浏览器或会话桥；两者保留来源识别，用于返回明确的失败原因。单条接口返回 HTTP 502，批量及异步任务对应项返回 `status=failed`、`complete=false`，结果字段均为 `null`。此工作区策略需发布后生效。

下表列出代码支持的字段路径，单篇未公开字段保留 `null`。评论只返回一级正文，回复仅保留数量；评论总数可能含回复。分页实现不等于已验证所有历史页。每个平台均经内容 ID、评论线程或目标页面绑定，拒绝推荐区计数及无依据的零值。

2026-11-01 微博连接恢复、头条评论浏览器恢复的工作区修改及隔离验证见 [修复记录](WEIBO_TOUTIAO_RECOVERY_2026-11-01.md)。该修改尚未部署；头条真实验证码的识别/提交/服务端通过链路尚未验证，不能理解为所有验证码均已解决。

| 规范媒体名 | 标识 | 实现文件 | 互动字段范围 | 评论及限制 |
|---|---|---|---|---|
| 知乎 | `zhihu` | [zhihu.py](../app/crawlers/platforms/zhihu.py) | 赞/评论/收藏；阅读为 null | 专栏零评论已实测；回答样本重定向到要求登录的安全验证页；非空及深页未通过实网验收 |
| 同花顺 | `tonghuashun` | [tonghuashun.py](../app/crawlers/platforms/tonghuashun.py) | 同顺号阅读/评论总数；财经新闻阅读/赞及开放时的评论总数 | 新闻编号按当前网页编码，校验 seq/encodedSeq/hotKey；不发送 inc；明确零评论可确认空列表，关闭评论保留未知，非空正文分页未验收 |
| 风闻 | `guancha` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 目标文章赞/评论总数；阅读未知 | 支持风闻社区别名及 main/wap 内容链接；6 条零评论样本已实测，非空正文分页未验收 |
| 即刻 | `jike` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 赞/评论/分享/转发 | 仅可确认目标明确为 0 的评论；非空正文未接通 |
| 玩车教授 | `chemm` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 页面显示阅读/赞/评论 | 阅读 1.4w 对应约 14000，不能使用 post_hits_real；62871 样本缺少有效目标状态；仅零评论 |
| 爱集微 | `ijiwei` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 阅读/赞/评论/收藏 | 兼容 laoyaoba.com/n/ 及分享链接；newlist 一级评论协议已接入；零评论已实测，非空/深页待实测；首屏省略 after_id |
| 简书 | `jianshu` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 阅读/赞/评论 | 首轮成功，复测部分响应缺少目标状态；仅能确认明确零评论 |
| 极客公园 | `geekpark` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 阅读/赞/评论 | 官网评论列表协议已接入；零评论已实测，非空待实测 |
| 潮新闻 | `tidenews` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 阅读/赞/分享/收藏 | 评论未接通；不能把前端默认空列表当零评论 |
| AcFun | `acfun` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 阅读/赞/评论/分享/收藏 | 非空根评论已实测；57 总评论含回复，返回 45 个根评论；分页协议已实现 |
| 上观 | `shobserver` | [regional_news.py](../app/crawlers/platforms/regional_news.py) | 阅读/赞/评论/收藏 | 上观/jfdaily 官方别名统一；零评论已实测，非空与深页待实测 |
| 顶端柘城 | `dingnews` | [regional_news.py](../app/crawlers/platforms/regional_news.py) | 阅读/赞/评论 | 顶端柘城公共 Web 签名，3 组字节对拍一致；仅零评论；不代表顶端新闻主站可用 |
| 澎湃新闻 | `thepaper` | [regional_news.py](../app/crawlers/platforms/regional_news.py) | 赞/评论；阅读为 null | 非空一级评论及第 2 页已实测；foldNum 和回复预览不是回复总数 |
| 新浪新闻 | `sina_community` | [sina_community.py](../app/crawlers/platforms/sina_community.py) | 赞/评论；阅读为 null | 目标页关联 qcmnt/ds 线程；非空与第 2 页已实测，过滤回复正文 |
| OFweek维科网 | `ofweek` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 评论数；阅读为 null | 校验 DetailID；只确认明确零评论，非空正文待验收 |
| 长沙晚报网 | `icswb` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 赞；阅读/评论未知 | 长沙晚报/掌上长沙统一；评论端返回空字符串，不能视作 0 |
| DoNews | `donews` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 目标页面阅读 | 评论未接通；不调用阅读量更新接口 |
| 增长黑客 | `growthhk` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 阅读/赞/评论 | 只取文章徽标，不取作者累计评论；仅零评论 |
| 第一电动网 | `d1ev` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 赞/评论；阅读为 null | 非空一级评论已实测，支持官网分页 HTML；深页待实测，过滤回复正文 |
| 数码之家 | `mydigit` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 帖子查看/赞/回复/收藏 | 零回复可确认；非空样本正文要求登录，明确返回 blocked |
| 42号车库 | `how42` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 赞/评论总数；阅读未知 | 安全解析 Nuxt 共享对象；viewCount 不作阅读数；官网 /view 评论接口已验证明确零评论，非空正文分页未验收 |
| 齐鲁壹点 | `ql1d` | [article_communities.py](../app/crawlers/platforms/article_communities.py) | 阅读/赞/评论 | 只解析目标 Nuxt newsData；仅明确零评论 |
| 潮起网 | `ichaoqi` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 显示阅读/评论 | 2.8w 是显示约数；只取文章元信息，排除热门侧栏；仅零评论 |
| 观海新闻 | `guanhai` | [publisher_pages.py](../app/crawlers/platforms/publisher_pages.py) | 赞；阅读/评论未知 | 只使用官网显示的 digg；不把未展示 pv=0 当真实阅读；评论请求未通过 |
| 紫牛新闻 | `yzwb` | [regional_news.py](../app/crawlers/platforms/regional_news.py) | 阅读/赞/评论 | 紫牛新闻 getDetail 校验 news_id；仅明确零评论；扬子晚报网旧门户为另一协议 |
| 抖音 | `douyin` | [douyin.py](../app/crawlers/platforms/douyin.py) | 赞、评论、分享、收藏；未公开播放数为 null | 公开一级评论分页；可能受登录/删除/风控限制 |
| 今日头条 | `toutiao` | [toutiao.py](../app/crawlers/platforms/toutiao.py) | 评论数；SSR 有效时补充点赞/分享等；仅有评论数时限时尝试浏览器补充，失败保留已核验字段 | 公开一级评论分页；参数无效及目标页面与接口共同确认不存在时停止重试 |
| 微信公众号 | `wechat` | [服务入口](../app/services/engagement_service.py) | 已停用，直接返回失败 | 不发起互动或评论采集 |
| 微信视频号 | `wechat_channels` | [服务入口](../app/services/engagement_service.py) | 已停用，直接返回失败 | 不发起互动或评论采集 |
| 小红书 | `xiaohongshu` | [xiaohongshu.py](../app/crawlers/platforms/xiaohongshu.py) | 阅读/赞/评/收藏/分享中实际公开字段；阅读常缺失 | 真实账号，本机 IP、无代理池；cursor，独立串行/15 秒间隔 |
| 好看视频 | `haokan` | [haokan.py](../app/crawlers/platforms/haokan.py) | 播放/赞/评论 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 快手 | `kuaishou` | [kuaishou.py](../app/crawlers/platforms/kuaishou.py) | 播放/赞/评论中公开字段；游客 SDK 经原代理初始化，目标 ID 严格匹配 | 公开一级评论分页；详情 1040 对应游客登录提示，21 对应失效；空响应不当作零评论 |
| 哔哩哔哩 | `bilibili` | [bilibili.py](../app/crawlers/platforms/bilibili.py) | 播放/赞/评/投币/收藏/分享/弹幕，依内容类型；动态支持 opus → desktop detail 的目标 ID 校验与游客会话 | 公开一级评论分页；动态按目标统计模块定位评论线程；可能受登录/删除/风控限制 |
| 微博 | `weibo` | [weibo.py](../app/crawlers/platforms/weibo.py) | 赞/评/转发；微博头条文章可有阅读数 | 可分页，深页可要求登录 |
| 新浪看点 | `sina` | [sina.py](../app/crawlers/platforms/sina.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 新浪财经头条号 | `sina_finance` | [sina.py](../app/crawlers/platforms/sina.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 百家号 | `baijiahao` | [baidu_publishers.py](../app/crawlers/platforms/baidu_publishers.py) | 公开阅读/赞/评论 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 有驾 | `yoojia` | [baidu_publishers.py](../app/crawlers/platforms/baidu_publishers.py) | 公开播放或阅读/赞/评论 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 搜狐号 | `sohu` | [sohu.py](../app/crawlers/platforms/sohu.py) | 文章 PV、评论；普通 MP 可有赞/收藏 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 懂车帝 | `dongchedi` | [dongchedi.py](../app/crawlers/platforms/dongchedi.py) | 公开阅读/赞/评论 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 今日头条微头条 | `weitoutiao` | [bytedance_publishers.py](../app/crawlers/platforms/bytedance_publishers.py) | 公开赞/评论/转发；阅读缺失为 null | 公开一级评论分页；可能受登录/删除/风控限制 |
| 西瓜视频 | `ixigua` | [bytedance_publishers.py](../app/crawlers/platforms/bytedance_publishers.py) | 公开播放/赞/评论等 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 网易号 | `netease` | [netease.py](../app/crawlers/platforms/netease.py) | 评论数；动态分享页可补充明确的非零点赞 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 雪球 | `xueqiu` | [xueqiu.py](../app/crawlers/platforms/xueqiu.py) | 赞/评/转发；隐藏阅读数为 null | 已切换官网 v3 评论接口；总数含回复，按目标 ID 校验一级正文并保留回复数；游标同时保留 max_id 和返回的排序类型；游客仍可能受筛选/验证限制 |
| 汽车之家 | `autohome` | [autohome.py](../app/crawlers/platforms/autohome.py) | 文章评论；论坛阅读/回复，依内容类型 | 文章分页；论坛过滤审核/仅作者可见回复 |
| 腾讯自选股 | `qq_stock` | [qq_stock.py](../app/crawlers/platforms/qq_stock.py) | 目标资讯公开互动字段 | 公开一级评论分页；可能受登录/删除/风控限制 |
| CSDN | `csdn` | [csdn.py](../app/crawlers/platforms/csdn.py) | 目标阅读/赞/收藏；评论数未知 | 公开页面解析通过；实网重复 521/连接失败，评论正文未上线 |
| 快资讯 | `kuai` | [kuai.py](../app/crawlers/platforms/kuai.py) | 评论数；其他字段为 null | 现代/旧分享 URL；零/非空根评论实测，深页待补实测 |
| 36氪 | `kr36` | [kr36.py](../app/crawlers/platforms/kr36.py) | 赞/评论数/收藏；阅读为 null | 目标文章明确零评论可确认空列表；非空评论正文未接通 |
| 选车网 | `chooseauto` | [more_car_news.py](../app/crawlers/platforms/more_car_news.py) | 阅读/赞/评论/分享/收藏 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 汽车网评 | `qcwp` | [more_car_news.py](../app/crawlers/platforms/more_car_news.py) | 阅读/评论；收藏状态不是收藏量 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 买车网 | `buycar` | [more_car_news.py](../app/crawlers/platforms/more_car_news.py) | buycar.cn 阅读/评论；maiche.com 可确认的非零赞数 | buycar.cn 零评论已实测；maiche.com 正文接口业务错误，明确受限 |
| 腾讯新闻企鹅号 | `qq_news` | [qq_news.py](../app/crawlers/platforms/qq_news.py) | 赞/评论/收藏/分享；阅读为 null | 公开一级评论分页；可能受登录/删除/风控限制 |
| 中关村在线 | `zol` | [zol.py](../app/crawlers/platforms/zol.py) | 评论数；阅读为 null | 公开一级评论分页；可能受登录/删除/风控限制 |
| IT之家 | `ithome` | [ithome.py](../app/crawlers/platforms/ithome.py) | 评论数；阅读为 null | 公开新文章可分页；旧文接口 endHint 限制必须报受限 |
| 什么值得买 | `smzdm` | [smzdm.py](../app/crawlers/platforms/smzdm.py) | 评论；文章头部明确的赞/收藏 | 不透明游标；非空首屏已实测，深页待补实测 |
| 爱咖号 | `xcar` | [xcar.py](../app/crawlers/platforms/xcar.py) | 目标浏览数/评论 | GBK JSON；非空评论和深页尚缺实网样本 |
| 财富号 | `caifuhao` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 易车号 | `yiche_hao` | [yiche.py](../app/crawlers/platforms/yiche.py) | 阅读/播放、点赞、评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 车家号 | `chejiahao` | [autohome.py](../app/crawlers/platforms/autohome.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 格隆汇 | `gelonghui` | [public_news.py](../app/crawlers/platforms/public_news.py) | 阅读/播放、点赞、评论数、分享、收藏 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 大鱼号 | `dayu` | [public_news.py](../app/crawlers/platforms/public_news.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 股吧 | `guba` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 一点资讯 | `yidian` | [public_news.py](../app/crawlers/platforms/public_news.py) | 点赞、评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 新浪汽车 | `sina_auto` | [sina.py](../app/crawlers/platforms/sina.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| ZAKER | `zaker` | [public_news.py](../app/crawlers/platforms/public_news.py) | 点赞、评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 汽车头条 | `qctt` | [car_news.py](../app/crawlers/platforms/car_news.py) | 阅读/播放、评论数 | 仅网页首屏 |
| 大风号 | `ifeng_dafeng` | [public_news.py](../app/crawlers/platforms/public_news.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 太平洋号 | `pcauto` | [car_news.py](../app/crawlers/platforms/car_news.py) | 阅读/播放、点赞、评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 新浪网 | `sina_news` | [sina.py](../app/crawlers/platforms/sina.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 水滴汽车 | `dripcar` | [car_news.py](../app/crawlers/platforms/car_news.py) | 阅读/播放、评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 车市号 | `cheshi` | [car_news.py](../app/crawlers/platforms/car_news.py) | 阅读/播放、点赞、评论数、收藏 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 牛客网 | `nowcoder` | [public_news.py](../app/crawlers/platforms/public_news.py) | 阅读/播放、点赞、评论数、分享、收藏 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 哈富证券 | `hafoo` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 天天基金 | `fund1234567` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 东方财富网 | `eastmoney_news` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 喜马拉雅 | `ximalaya` | [public_news.py](../app/crawlers/platforms/public_news.py) | 阅读/播放、评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| Choice数据 | `choice_data` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 凤凰网 | `ifeng` | [public_news.py](../app/crawlers/platforms/public_news.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| choice | `choice_wealth` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 网通社·汽车 | `news18` | [car_news.py](../app/crawlers/platforms/car_news.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 新浪财经 | `sina_market` | [sina.py](../app/crawlers/platforms/sina.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 网易视频 | `netease_video` | [netease.py](../app/crawlers/platforms/netease.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 新浪科技 | `sina_tech` | [sina.py](../app/crawlers/platforms/sina.py) | 评论数 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 搜狐网 | `sohu_news` | [sohu.py](../app/crawlers/platforms/sohu.py) | 阅读/播放、点赞、评论数、收藏 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 东方财富 | `eastmoney` | [eastmoney.py](../app/crawlers/platforms/eastmoney.py) | 阅读/播放、点赞、评论数、分享 | 公开一级评论分页；可能受登录/删除/风控限制 |

## 名称与链接

- `腾讯网`、`腾讯新闻`、`腾讯新闻企鹅号` 使用 `qq_news`；视频分享会核对页面中的 ID 映射。
- `微博头条` 复用微博文章路径；`爱卡汽车爱咖号` 复用 `xcar`。
- 中关村在线支持移动 canonical；水滴汽车支持无 `www` 域；汽车之家新增论坛 URL；爱咖号支持图文/视频分享 URL；网易动态保留 `/news/rec/` 原地址及独立跟帖产品。
- 原始名称未必等于同名已接入协议：易车经销商页不等于易车号，新浪新闻 `super.sina.cn/shequn` 不等于新浪新闻文章，证券 App 壳页不等于东方财富帖子。不会仅按名称近似强行映射。
- 路由事实源是 [registry.py](../app/crawlers/platforms/registry.py) 与 [source_catalog.py](../app/crawlers/platforms/source_catalog.py)，运行中的注册范围可查询 `/api/v1/health`。

## 接口与维护

请求/响应见 [评论 IO](COMMENTS_API_IO.md)、[API](API.md)。架构、缓存、代理和任务分工见 [执行流程](PLATFORM_FLOWS.md)。

新增来源必须同时对齐 `EngagementPlatform`、注册处理器、中文名称和评论能力；扩展来源统一从 `EXTENDED_SOURCES` 派生后三项。不要通过注册“始终空成功”的处理器凑平台数。
