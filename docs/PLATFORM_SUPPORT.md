# 平台与采集器清单

更新：2026-10-31。当前代码注册 **61 个规范来源名称**（原 50 + 新增 11），包含共享协议和仅支持部分字段的来源。**61 不是 61 个平台全字段、全评论均可用**。原 20,000 条导出的前 80 名及未接入项逐条见 [覆盖与待办](PLATFORM_BACKLOG.md)，本轮实测见 [扩展验收](PLATFORM_EXPANSION_2026-10-31.md)。

调用方继续提供 `url + media_name`；小红书无需额外提交作者或访问参数。内部恢复访问链接仍取决于已有会话、可核验线索及作品可见性，登录不保证任意裸链接都能恢复。

下表列出代码支持的字段路径，单篇未公开字段保留 `null`。评论只返回一级正文，回复仅保留数量；评论总数可能含回复。分页实现不等于已验证所有历史页。每个平台均经内容 ID、评论线程或目标页面绑定，拒绝推荐区计数及无依据的零值。

| 规范媒体名 | 标识 | 实现文件 | 互动字段范围 | 评论及限制 |
|---|---|---|---|---|
| 抖音 | `douyin` | [douyin.py](../app/crawlers/platforms/douyin.py) | 赞、评论、分享、收藏；未公开播放数为 null | 公开一级评论分页；可能受登录/删除/风控限制 |
| 今日头条 | `toutiao` | [toutiao.py](../app/crawlers/platforms/toutiao.py) | 评论数；SSR 有效时补充点赞/分享等 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 微信公众号 | `wechat` | [wechat.py](../app/crawlers/platforms/wechat.py) | 页面明确公开的赞/推荐/精选评论数；阅读量不保证 | 匿名正文不可用 |
| 微信视频号 | `wechat_channels` | [wechat_channels.py](../app/crawlers/platforms/wechat_channels.py) | 公开预览的赞/评论/转发/收藏；客户端链接受限 | 匿名正文不可用；仅公开预览计数 |
| 小红书 | `xiaohongshu` | [xiaohongshu.py](../app/crawlers/platforms/xiaohongshu.py) | 阅读/赞/评/收藏/分享中实际公开字段；阅读常缺失 | 真实账号，本机 IP、无代理池；cursor，独立串行/15 秒间隔 |
| 好看视频 | `haokan` | [haokan.py](../app/crawlers/platforms/haokan.py) | 播放/赞/评论 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 快手 | `kuaishou` | [kuaishou.py](../app/crawlers/platforms/kuaishou.py) | 播放/赞/评论中公开字段 | 公开一级评论分页；可能受登录/删除/风控限制 |
| 哔哩哔哩 | `bilibili` | [bilibili.py](../app/crawlers/platforms/bilibili.py) | 播放/赞/评/投币/收藏/分享/弹幕，依内容类型 | 公开一级评论分页；可能受登录/删除/风控限制 |
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
| 雪球 | `xueqiu` | [xueqiu.py](../app/crawlers/platforms/xueqiu.py) | 赞/评/转发；隐藏阅读数为 null | 公开一级评论分页；可能受登录/删除/风控限制 |
| 汽车之家 | `autohome` | [autohome.py](../app/crawlers/platforms/autohome.py) | 文章评论；论坛阅读/回复，依内容类型 | 文章分页；论坛过滤审核/仅作者可见回复 |
| 腾讯自选股 | `qq_stock` | [qq_stock.py](../app/crawlers/platforms/qq_stock.py) | 目标资讯公开互动字段 | 公开一级评论分页；可能受登录/删除/风控限制 |
| CSDN | `csdn` | [csdn.py](../app/crawlers/platforms/csdn.py) | 目标阅读/赞/收藏；评论数未知 | 公开页面解析通过；实网重复 521/连接失败，评论正文未上线 |
| 快资讯 | `kuai` | [kuai.py](../app/crawlers/platforms/kuai.py) | 评论数；其他字段为 null | 现代/旧分享 URL；零/非空根评论实测，深页待补实测 |
| 36氪 | `kr36` | [kr36.py](../app/crawlers/platforms/kr36.py) | 赞/评论数/收藏；阅读为 null | 评论正文未上线 |
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
