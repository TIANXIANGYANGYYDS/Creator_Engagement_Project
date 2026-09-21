# 作者采集 v6：仅访客模式与实测

本文记录 v6 的历史验收；后续 B 站连续分页、自动 Cookie 研究及 v7 实测见 [自动 Cookie 版本验收](AUTHOR_AUTO_SESSION_2026-09-19.md)。

2026-09-19，根据最新要求，作者采集流程不涉及真实账号。输入保持 `author_id`、`media_name`；视频采集公开标题、简介、链接等，文章采集正文。允许使用动态代理和浏览器，作品总数及代理消耗总次数不设上限。

## 本次实现

- `AUTHOR_GUEST_ONLY=true` 默认开启，优先于抖音、小红书和其他作者 Cookie 配置及历史浏览器会话。作者 crawler 不读取保存的登录状态；每个列表/详情作用域使用隔离 HTTP 会话，浏览器从空状态启动，只接收站点新生成的访客 Cookie。
- 抖音来源作品映射导入脚本同步支持仅访客浏览器核验，不再以账号 Cookie 为前置条件。映射仍须在线验证作品 ID 与作者账号，不能按昵称猜测。
- 列表每页保留 120 秒时限和三次即时尝试；临时错误用新会话从持久化游标自动重试，退避为 5、10、20、40、60 秒。等待释放作者执行槽和全部采集/浏览器/代理资源。状态新增 `retry_count`、`next_retry_at`、`phase=retry_wait`。
- 单次任务运行窗口为 24 小时，截止时间持久化；进程重启保留期限，显式 resume 开始新的窗口。作品总量与代理次数没有限制。调用方可以取消；失败详情仍支持独立续跑。
- 身份不匹配、非法游标、明确登录限制不会进入持续重试。登录限制不当作空作者、末页或成功。模糊网络/签名失败与明确登录限制分别处理。
- 修复 B 站多来源重复作品被误判为分页卡死：视频投稿和动态可以返回相同作品，存储去重后继续；同一来源重复整页或游标循环仍报错。

## 无账号真实验证

以下来自实际请求，不是模拟响应测试。来源为 `.local/top20_platform_data.json` 的真实作者。原始规范化结果及失败摘要在 `.local/analysis/guest-authors/`。

| 平台 | 本轮观测 | 未通过的部分 |
| --- | --- | --- |
| 抖音 | `ChenNan12.24` 首屏 20 条，`46219502311` 首屏 23 条；全新浏览器取得访客会话。打开前者主页能核对抖音号和作品总数 306。 | 后续游标返回缺少列表的数据包；换出口有连接/UIFID 初始化失败。Camoufox 主页滚动后第二页仍未取得；另测全新 Chromium 返回验证页。 |
| B 站 | `1804532025`、`1187120568` 各首屏 30 条。 | 第二页 `-352/412`；测试额外公开请求参数和旧接口未解决。Camoufox/Chromium 主页路径在当前出口遇到连接/代理隧道失败。 |
| 微博 | `7002783993` 首屏 10 条；同一会话重复首屏成功。 | 第二页失败；换 IP 直接从第二页开始仍失败，不能简单认定 IP 过期。`7727451889` 明确返回登录提示。 |
| 快手 | 保留从公开移动分享页核验数字 ID 和主页 ID 的实现。 | `1761218769` 列表 REST/GraphQL 返回 50 或连接失败；公开直播主页未取得有效作品列表。 |
| 小红书 | 实际内部 UID 主页三次能够连接，但内容是未登录页面。 | 页面初始化数据为空不能视作空作者；用户输入小红书号的解析/列表还遭遇连接失败。 |
| 今日头条 | 部署后纯访客作者 `3402362465301307` 连续读取 25 页、保存 245 条视频；结果读回 245 个唯一 ID，245 条内容完成、失败详情 0，来源明确结束。 | 首组探测曾遇到连接失败、非 JSON；成功样本证明该作者当前公开来源可遍历，不能保证任意作者或长期稳定。 |

此前 v4 抖音 19/20 作者、61 页、1,117 条结果使用了账号 Cookie，**不属于无账号验收结果**。v5 允许账号会话的部署也已被本次要求取代。当前仍不能声明六个平台无账号稳定采集全部历史已经实现。

## 上游参考核查

- [Douyin_TikTok_Download_API 排错文档](https://douyin.wtf/troubleshooting/)记录了非零 `max_cursor` 只返回状态包、首屏正常的现象。与本轮表现相似，但不能由此断言所有失败都只有一个原因。
- [RSSHub B 站视频路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/bilibili/video.ts)使用更多 Web 参数、设备信息及浏览器回退；本轮检查了相关参数，尚未证明可解除当前出口的后续页失败。
- [RSSHub 快手公开主页路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/kuaishou/profile.ts)使用主站预热和直播主页公开请求；本轮复测未取得有效列表，没有把空响应接成“全部采完”。
- [douyin-live-recorder 移动列表实现](https://github.com/Ivan494/douyin-live-recorder/blob/main/src/douyin_media_downloader.py)的移动作品列表明确依赖 App 会话 Cookie，不能直接作为无账号解法。未引入其账号依赖或 30 页上限。

## 回归检查

`CREATOR_RUN_MONGO_TESTS=1 /home/txy/miniconda3/envs/MyAgent/bin/python -m pytest -q`：**344 passed**。包括账号隔离、保存游标自动恢复、重试等待释放执行槽、重试取消、重启保留截止时间、B 站跨来源重复及同来源重复检测。Python 编译和 `git diff --check` 通过。自动化通过不代表平台实网限制已经解决。

部署有效模式应为 `author_pipeline_version=6`、`author_guest_only=true`、`author_use_saved_sessions=false`、`douyin_author_session_mode=anonymous`。实际部署时间、PID、回滚快照与接口验证保存在本机 `.local/analysis/guest-authors/`，文件不包含平台账号凭据。

2026-09-19 14:47:29（Asia/Shanghai）已切换 8200 服务，PID `2371951`，切换前没有活动任务。回滚快照为 `.local/releases/20260919-144728-author-guest`。健康检查四项模式值均正确，批量总数限制继续为 0。

真实 HTTP 批量提交三个作者在约 8 毫秒内返回 202，业务请求仍只含 `author_id`、`media_name`。头条任务 `author_b1bd801659da4ac8903399e55a5777e5` 达到上述 245 条，`discovery_done=true`；`status=partial` 是公开历史覆盖无法证明，不表示详情仍待处理。抖音任务保存首屏 20 条并进入 `retry_wait`，观测到 `retry_count=1` 和下一次重试时间后，主动取消验收任务，20 条结果保留。微博明确登录限制在运行后及时返回失败。记录见 `.local/analysis/guest-authors/deployed-verification.json` 和对应 `deployed-results-*.json`。

另以头条混合内容作者 `100515238009` 验证列表与多代理详情：约三分钟验收窗口内保存 41 页、492 个唯一作品 ID。主动取消时，23 条详情完成（10 篇文章、13 条视频），17 条失败，452 条仍待处理；这是限时抽检后主动停止，不能把 492 条全部算作已取得正文。10 篇文章正文长度为 1,509～3,917 字符。详情失败包括正文不可用、缺少可核验结构和页面连接失败，保留后续 resume 所需上下文。记录为 `.local/analysis/guest-authors/articles-deployed.json` 及 `articles-deployed-results.json`。验收结束后活动作者任务数为 0。
