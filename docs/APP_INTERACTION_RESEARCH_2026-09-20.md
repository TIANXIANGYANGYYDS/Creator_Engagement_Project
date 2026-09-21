# App 路径研究与诊断（2026-09-20）

## 2026-09-21 状态复核：仅互动和评论

用户已明确不需要作品正文；微信公众号即使只有真实评论计数，也可算有效互动结果。
本次从业务 JSON 的三个平台各取第一条原始链接，分别请求线上 8200 和当前工作区的互动接口。
两套版本均为小红书缺有效 `xsec_token`、公众号验证码、视频号客户端跳转链接缺可用会话，全部 HTTP 502。
公众号“仅评论数”的最低目标仍未通过。不能将这些抽样结果表述为已遍历全部业务链接。

独立对照的小红书 token 链接返回 likes=3657、comments=2162、shares=6907、favorites=725。
视频号 `sph` 对照此前返回四项计数，本次为请求失败。线上健康检查仍为 HTTP 200，未重启服务。
脱敏结果：`.local/analysis/interaction-h5-20260920/status-20260921.json`。

上轮继续调查得到的证据：

- 普通 Android Chrome、iPhone Safari 请求原始小红书链接仍进入安全拦截页；去掉微信 UA 只改变跳转目标，没有取得互动数据。
- 视频号官方 `feed.db97a0d4.js` 与 `merlin.82141f20.js` 调用 `feed/get_feed_info`，以 `shortUri` 或 `exportId` 加 `generalToken` 取预览。已检查模块没有评论列表请求；这不代表所有客户端入口都不存在评论接口。
- [TikHub 官方接口描述](https://api.tikhub.io/openapi.json) 的 `fetch_article_stats_h5` 声明只返回评论/精选评论计数，其余指标为空。该服务需要 API 凭据，未取得其底层实现、未调用或采购；文档声明不能当作本项目已验证的能力。
- [MediaCrawler 小红书实现](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py) 使用 `xsec_token` 和有签名的评论分页接口，没有解决当前原始链接缺少有效 token 的前置问题。

针对评论返回的具体修复已写入工作区：小红书明确返回空列表的末页不再误判失败，缺列表或无法识别的非空列表仍失败；
公众号 `ret=0` 的空壳响应不再当成有效评论页，明确的零计数不会被其他计数覆盖；
公众号新版 `reply_new.reply_total_cnt` 被映射为回复数，保留真实零值。
这些修复改善结果正确性，不解决平台会话获取。完整回归为 **385 passed、22 skipped**，`git diff --check` 通过，尚未部署。
诊断及测试均经现有 5 GB、无 swap 的资源限制器执行。

业务输入仍为 `.local/top20_platform_data.json` 中的原始 URL 与 `media_name`，重点为小红书、微信公众号和微信视频号。目标是取得至少一个真实互动字段，并继续验证评论能力。

### 2026-09-21 原始链接 App 路径续测

本次仍取业务 JSON 的小红书原始链接
`https://www.xiaohongshu.com/discovery/item/6aad6b8c0000000028002d0a`，没有替换成带
`xsec_token` 的对照链接。用户提供的手机截图显示未登录界面存在互动数和部分评论；该截图
证明其手机环境可显示这些内容，但没有提供对应业务 URL，因此截图中的计数不作为样例验收结果。

Android 11 x86_64 + ARM64 NDK translation 实验已得到以下证据：

- 定向调整实验中该包的进程附加等待后，App 可以进入首次提示页及 `RouterPageActivity`。
  早期包装启动实验与正常启动实验不是同一次：包装启动另有原生库架构错误，不能合并成成功。
- 已读取实验 App 自有的 MMKV 状态，按长度编码解析后确认 `is_privacy_policy_granted=true`。
  这说明“未点击同意”不是本次卡住的全部原因；只搜索到键名不足以得到此结论。
- Frida 客户端已附加，但本次没有收到 `hook_ready` 或 `capture_ready`；`Java.perform`
  中的请求拦截器没有确认就绪。旧文件里的准备事件来自之前的运行，不能与本轮混用。
  因此不能把“观察器没有响应”解释为“App 没有请求网络”。
- 两次 App 进程均出现 `SIGSEGV/SEGV_ACCERR`。第一次 native 栈包含系统传感器到 Java
  的转换；第二次启动没有重新附加 App 观察脚本，但系统服务中的超时调整仍在。
  当前证据不足以区分架构转换、App 兼容性或运行时保护，不能断言具体根因。
- 没有取得目标详情或评论业务响应。760 秒左右的受控实验峰值约 4.3 GiB，swap 为 0，
  结束后实验进程已停止；没有修改或重启 8200 服务。

静态分析确认官方 APK 分享内容接口 `/api/growth/schubert/share/content` 接收
`ShareContentRequest`。进一步追踪 `ShareSpiProxyImpl.copyLink` → `m77.f` → `qxb.p.e` →
`m77.i` 后，确认普通笔记的 `businessName=NOTE`、`subBusiness=NORMAL_SHARE`，
`shareObjectId` 直接取自 `NoteItemBean.getId()`；复制链接分支的 `shareType=COPY`。
因此原始 URL 中的笔记 ID 可以提供这个字段，先前“URL 不能提供 shareObjectId”的判断不成立。
`shareId` 的生成规则和 `shareChannel` 的映射仍需继续核对，当前原生会话及签名尚未验证。
脱敏调用链保存在 `share-contract-verified.json`，完整反汇编保存在 `share-context-callers.json`。

同一原始笔记 ID 的兼容性请求已执行：自动取得并核验网页游客会话后，分别不带网页签名、
带 `xyw` 网页签名调用该分享接口，均为 HTTP 200、业务码 `-100`、`success=false`、
“登录已过期”，没有 `data`。这不是完整 App 原生请求复现：会话/签名仍属网页协议，
`shareId` 和 `shareChannel` 尚有未核对取值，不能据此认定 App 游客不可用。
结果保存在 `share-probe-results.json`，没有接入生产采集器。

ARM 环境对照也没有形成业务成功：Android 7.1 镜像在 Emulator 37.1.11 和 32.1.14 下均出现
`zygote64` 的 `artQuickResolutionTrampoline` 崩溃，关闭即时编译的对照仍失败。
Android 8.1 原配置因无 PCI 总线而无法创建 HDA 音频设备；单独实验目录中下调启动器看到的
API 元数据，绕过 HDA 创建后，系统进入 `system_server`，但音频 HAL 反复崩溃并阻塞系统启动。
该改动只影响本地测试镜像的启动元数据，未修改 APK 或平台请求。没有在这组 ARM 环境完成
安装、启动小红书，不能将它计入 App 内容请求。最后一次实验约 202 秒、峰值约 2.6 GiB、
swap 为 0，已停止。仍需可稳定运行目标 App 的 Android 环境，不能继续把启动次数当作采集进展。

网页采集器的错误提示已限定为“当前网页路径缺少参数或没有数据”，不再要求调用方改写业务
原始链接，也不将网页失败扩大为“App 游客必须登录”。这些文案修正不改变接口成功率或返回结构。

资源限制器新增 `--memory-mb`，允许轻量任务在 128–5000 十进制 MB 范围内下调额度，
默认仍是 5 GB。实际 128 MB 子进程核验了 `memory.max=128000000`、
`memory.high=102400000`、`memory.swap.max=0`、10 核 CPU 配额和禁止 core dump。
0、127、5001、负数四类无效额度均在启动 unit 前被拒绝。512 MB 下载任务峰值约 236.5 MiB。
该调整解决小任务按完整模拟器额度预留内存的问题，不代表新增采集能力。

本轮脱敏证据位于 `.local/analysis/raw-links-20260921/`：
`consent-verification.json`、`guest-capture-status.json`、`small-resource-verification.json`。
原始 logcat、崩溃堆栈和设备状态只保存在本机受限目录。
本轮相关回归 `162 passed`，语法检查与 `git diff --check` 通过；这不包含在线平台成功验收。

公众号的简化评论请求继续返回 `no session`，视频号原始 `feedID` 链接仍缺少
`generalToken`/客户端会话。当前三类业务原始链接均没有取得新的可验证互动字段；生产接口仍
按失败返回，不伪造评论数或把会话初始化成功当成数据成功。

已完成公开源码核对、小红书 App Shield 算法离线验证、原始样例的 App 评论路径探测，并进一步分析官方 9.47.0 APK、搭建 Android/Frida 测试环境。网页协议已经能够用全新设备标识自动激活游客会话，并通过 `user/me` 确认 `guest=true`；这只证明会话初始化成功，原始样例的详情和评论仍未返回可验证数据，因此没有将游客初始化直接接入生产采集器。

本轮还为受控的原生分析增加了 `python -m app.manually_execute_script.run_limited_analysis -- COMMAND ...`。该命令把分析进程树放进独立 systemd cgroup：`MemoryMax=5,000,000,000`、`MemorySwapMax=0`、`CPUQuota=1000%`（最多 10 个核），并保留 2 GiB 主机内存和 5 GiB 磁盘余量；8200 健康检查连续失败两次会停止实验。它只约束显式通过该命令启动的分析任务，不改变常驻 API 服务的资源上限。

## 继续验证：官方 APK、运行环境与 reverse-skill

### 已确认的静态结果

从官方下载安装入口取得小红书 9.47.0（build `9470809`）APK，包名 `com.xingin.xhs`，包含 ARM64 native 库。下载来源、包信息与校验记录位于 `.local/analysis/native-bootstrap-20260920/`。

- `ILoginService.apiSnsV1UserActivatePost` 对应 `/api/sns/v1/user/activate`，当前版本的请求包括 23 个表单字段，其中有设备参数、安装状态和 `client_public_key_base64`。
- `AccountSskClient` 与 `X25519KeyExchange` 参与会话密钥处理；反汇编显示先进行 X25519 交换，再以共享密钥对返回材料执行 AES-GCM 解密。
- `com.xingin.shield.http.Native` 包含 `initialize`、`intercept` 与 `updateCachedSsk` 等 native 方法，核心实现位于 `libxyass.so`。因此不能把旧版 Python Shield 的空 HMAC 输出直接当成当前版本可用的完整游客流程。

这些结果确认了客户端代码的处理路径，尚未取得 App 原生接口返回的有效游客会话，不能据此宣称采集已成功。下述网页游客会话属于另一条已验证的初始化路径。

### 网络故障与业务校验分开记录

进一步直接检查代理的 HTTP CONNECT 响应时发现：访问 `edith.xiaohongshu.com` 会收到代理层 `403 Forbidden`，带 `Proxy-Agent` 响应头；另用 6 个新代理测试该主机的普通 HTTP 请求，也全部返回代理层 403。同一代理池可以访问 `www.xiaohongshu.com` 和对照站点。

这说明此前部分 `ProxyError` 发生在代理连接阶段，不能当作平台签名或游客校验结果。客户端最初只对该主机、HTTPS 443、代理 CONNECT 403 这一窄条件执行 DNS IPv4 解析，并用 libcurl `CONNECT_TO` 让同一代理连接到解析地址；不会重放源站 403。后续依据实测将相同恢复条件扩展到 `res.wx.qq.com`、`webapi.rednote.com`，详见下方续测记录。生产环境的 `proxy_mode=required` 保持不变。

修复传输层后，真实请求得到的业务结果是：`/api/sns/web/v1/login/activate` 返回游客 `user_id/session`，随后 `/api/sns/web/v2/user/me` 返回 `guest=true`；原始笔记详情、评论页和账号搜索仍分别返回空数据或权限限制。游客会话模块和 CLI 因此只报告“会话已验证”，不会伪造互动数或把空评论页当成成功。

可复用流程位于 `app/crawlers/xiaohongshu_guest.py`。以下命令使用项目现有代理配置，最多尝试三次，每次以独立 Cookie 容器和同一代理完成激活、身份核验；默认不保存 Cookie：

```bash
python -m app.manually_execute_script.bootstrap_xiaohongshu_guest
# 需要交给后续本地诊断时，才写入权限 0600 的 Playwright storage state：
python -m app.manually_execute_script.bootstrap_xiaohongshu_guest \
  --output .local/analysis/xhs-guest-state.json
```

真实命令返回 `ok=true, guest=true, account_used=false, content_access_verified=false`，已核验输出文件权限为 0600。保存的游客状态是临时材料，尚未验证换 IP 或过期后的可用性，也未作为生产账号会话注入。

### Android 与 Frida 实测状态

- 已在项目忽略目录安装 Android platform-tools、Emulator 和 Android 11 Google APIs x86_64 系统镜像；镜像声明支持 ARM 应用转换。
- 本机没有 `/dev/kvm`，采用软件模拟。系统能够启动，ADB 能连接，包管理器能查到 APK 路径，但安装后的 DEX 校验持续运行，应用没有进入前台进程；这次实验在 5 GB cgroup 约束下结束。因此“能查到安装包”不等于 App 已正常运行。
- Frida 17.18.0 客户端和 Android server 的连接已验证，可以列举模拟器进程；未观察到稳定运行的小红书 App 进程，尚未抓取到原生详情/评论响应。
- 当前限制属于本机动态分析环境，不是“小红书游客必须登录”的证据。后续应先解决原生 App 稳定运行，再比较客户端与协议请求。

### 用户指定技能的安装与调用

已安装用户指定的 [zhaoxuya520/reverse-skill](https://github.com/zhaoxuya520/reverse-skill)，固定源码提交 `7e2097fd90d25c2f976f6eba26d6c00aa88051df`。

- Codex 入口：`/home/txy/.codex/skills/reverse-skill-router/SKILL.md`。
- 完整仓库：`/home/txy/.codex/vendor/reverse-skill`。
- 已在 `/home/txy/.codex/AGENTS.md` 写入 App 逆向时使用该技能的用户偏好，并提供完整仓库路径。
- Codex `skills/list` 实测返回 `enabled: true`、`scope: user`，无发现错误。
- 当前任务实际路由到 `skills/apk-reverse/SKILL.md`；仓库自带的 175 个路由用例和默认输出目录回归全部通过。
- 已生成工具索引并加载 APK 工作流；此次安装没有额外注册 MCP 服务。

技能安装记录位于 `.local/analysis/reverse-skill-install/installation.json`，任务范围记录位于 `.local/analysis/native-bootstrap-20260920/work/xhs-guest/scope.md`。技能可用与业务采集成功是两个独立验收项，后者仍未通过。

## 对之前结论的限定

之前无 token 小红书链接失败、公众号验证码和视频号客户端跳转页不含统计，属于已测试网页及会话桥路径的结果，不能据此断言 App 游客也无法读取。

用户不输入账号，客户端仍可能维护设备标识、游客会话、签名材料。若内容是在已登录微信内部打开，文章没有再次要求登录，也不代表请求没有使用微信会话。用户实际打开入口尚未确认，不能把这两个情形混为一谈。

## 找到并检查的实现

| 来源 | 可以借鉴的部分 | 尚不能证明的能力 |
| --- | --- | --- |
| [RedNote/Xiaohongshu-Shield-Algorithm](https://github.com/RedNote/Xiaohongshu-Shield-Algorithm) | Python 实现 native Shield；README 标称 Android 9.19.2，测试日期 2026-03-10 | 仓库只包含签名实现、README 和许可证，不包含完整设备初始化及游客采集流程；不能视为当前版本全流程可用 |
| [zhkl0228/unidbg](https://github.com/zhkl0228/unidbg) | 在服务器模拟 Android ARM32/ARM64 native 库、JNI 调用，作为提取或复用 App 算法的工具 | 通用执行环境，不附带小红书当前版本的设备注册、签名参数和可用会话 |
| [hjyl-cheng/wechat-pcspider](https://github.com/hjyl-cheng/wechat-pcspider) | Windows 微信自动打开文章，捕获 Cookie、key、uin、pass_ticket，再提取文章统计与留言 | README 明确要求已登录的 PC 微信；自动捕获凭据不等于自动获得真实账号登录态 |
| [nobiyou/wx_channel](https://github.com/nobiyou/wx_channel) | 注入微信页面，通过 `finderGetCommentDetail` 取详情，通过 `finderGetCommentList` 与 `lastBuffer` 取评论分页 | 需要运行中的微信页面 API；本机没有对应客户端会话，未完成真实样例验证 |

公开的 [Shield 作者商业实现说明](https://rnote.dev/en/source) 另外列出设备注册、游客状态和 `x-mini-*` 等参数生成能力；这些是提供方的产品描述，未取得源码或测试账号，本项目未验证，也未采购。不能将商业页面的能力声明当作开源仓库已经提供的功能。

## 小红书：离线发现与联网结果

下载源码 SHA256：`25dfdfd6dc6c43377f55f981a7950b80da98a1ae6e92c926b82a668bcef20c10`。

诊断使用自行生成的设备 UUID，不使用仓库示例设备凭据、HMAC 或其他人的会话。`loguru` 仅安装到忽略目录内的诊断依赖路径，没有修改服务依赖；关闭上游初始化日志，避免输出签名材料。

### 无 HMAC 的输出不能当作已验证签名

| 离线输入 | 不同请求字符串数 | 不同输出数 | 解释 |
| --- | ---: | ---: | --- |
| 同一设备，空 HMAC | 5 | 1 | 能生成 134 字符、以 XY 开头的字符串，但输出不随请求变化 |
| 同一设备，本地产生的合成 HMAC | 5 | 5 | 仅证明含 HMAC 分支参与请求摘要计算，不代表服务端认可 |

源码中 `XYAes.main()` 在空 HMAC 时返回 `None`，`XYMd5.main()` 随后返回 16 个零字节。故空 HMAC 输出没有绑定请求内容。它是否适用于某种初始化请求仍待验证；不能直接用于证明详情、评论接口签名已打通。合成 HMAC 只用于离线对照，从未发送给平台。

### 对原始样例的探测

源文件第 14 条（零基索引 13），笔记 ID `6aad6b8c0000000028002d0a`。

请求路径参考开源签名项目：`/api/sns/v5/note/comment/list`，分别测试不带 Shield 和空 HMAC Shield。查询中使用目标笔记 ID，无 `xsec_token`；保持项目强制代理要求。

| 主机 | 变体 | 结果 |
| --- | --- | --- |
| edith.xiaohongshu.com | 不带 Shield | ProxyError，curl code 56，未取得平台业务响应 |
| edith.xiaohongshu.com | 空 HMAC Shield | ProxyError，curl code 56，未取得平台业务响应 |
| www.xiaohongshu.com | 不带 Shield | HTTP 406，无评论数据 |
| www.xiaohongshu.com | 空 HMAC Shield | HTTP 406，无评论数据 |

两个 406 响应虽然带 `code: 0, success: true`，但无数据，不能算作采集成功。未复现真实设备初始化、`x-mini-*` 参数及 App TLS 指纹，传输使用项目现有 Chrome 指纹客户端。因此这只是 App 接口路径的初步协议探测，不是完整 App 游客验证，也不能据此断言游客必须登录。

## 微信客户端路径

公众号参考源码 `wechat_automation.py` 依赖 `pywinauto` 查找微信窗口；`capture_new_wechat.py` 从客户端请求捕获有效会话参数。这条思路可以用于补充当前文章统计流程，但它的运行条件包括已登录微信，无法仅靠增加代理数量提供该条件。

视频号参考源码 `internal/assets/inject/api_client.js` 的详情参数支持 `eid / encryptedObjectId / encrypted_objectid`，组装 `encrypted_objectid`、`scene=141` 后调用页面的 `finderGetCommentDetail`。这与业务文件中 `export/...` 的 feedID 路径相关，值得通过客户端实测；当前项目已经存在相应侧车适配入口，尚缺运行中的微信客户端。

视频号评论文档明确使用 `finderGetCommentList`、`object_id`、`nonce_id` 和分页标记。能力来自微信页面 API，而非 `mobile/commonFinderJsApi.html` 跳转 HTML 本身。

## 本机条件与后续验收

原生模拟器实验观测到 4,003,115,008 字节（约 3.73 GiB）的 cgroup 峰值，没有超过 5,000,000,000 字节，swap 峰值为 0；结束实验后资源已释放。停止时模拟器报告 SIGSEGV，仍未形成可用的 App 动态采集流程。该峰值说明需要继续把 App 动态分析作为受控、单任务操作，不能同时启动多个模拟器。

资源守护实测：在测试命令上故意指定不可用的健康检查地址，两次失败后约 2 秒自动终止整个分析 unit，并返回 125；正常健康地址下测试命令按预期退出。8200 健康接口持续返回 `status=ok`。限制脚本还禁用 core dump，避免实验崩溃额外写出大型内存镜像。

子进程直接读取自身 cgroup 也确认限制已生效，证据为 `.local/analysis/native-bootstrap-20260920/resource-verification.json`。内核把内存上限按页向下取整，因此实际 `memory.max` 稍低于配置的 5,000,000,000 字节；`memory.swap.max=0`、CPU 配额/周期比为 10、core 文件软硬限制均为 0。

本轮完整测试为 351 passed、22 skipped；随后增加取消/失败时关闭恢复连接及 `discard_cookies` 的测试，HTTP 与游客会话相关测试为 39 passed。跳过项仍按现有测试环境条件跳过，不能当作已通过的在线平台验收。本轮代码在工作区，8200 尚未切换该版本。

最初检查时，本机 PATH 未发现 `adb`、`frida`、Android `emulator`，常见 Android SDK 目录不存在；随后已在忽略目录安装上述诊断工具，运行结果见前文。仍未发现 `/dev/kvm` 和 USB 设备目录。Docker 客户端存在，但当前用户无法访问 Docker socket；本机尚没有已经跑通目标 App 的动态分析环境。

下一步应优先复现小红书的完整游客流程：全新 App 数据目录、不登录账号，打开同一原始 URL，记录设备初始化和详情/评论请求，先验证至少一个互动字段，再验证评论分页。能否用 `unidbg` 或纯协议替代客户端，应以这条成功请求作为对照。

设备初始化与后续请求先保持同一设备状态和同一代理出口，得到基准结果后，再测试设备状态是否允许跨 IP 复用。允许消耗更多 IP 可以扩大并发及替换失效出口，但不能代替缺失的客户端会话；当前尚未验证这些 App 参数与 IP 是否绑定。

业务成功标准保持不变：目标 ID 对应正确、响应含真实字段、原始 URL 无需调用方补参数；网络失败或缺字段不能填成零；首批评论条数不能冒充总评论数。

## 本地证据

位于 `.local/analysis/app-path-research-20260920/`：

- `sources.json`：已下载源码来源及校验值。
- `shield_sdk.py`、`shield-readme.md`：上游签名实现及说明快照。
- `probe_native.py`、`native-results.json`：4 次 App 路径探测及边界说明。
- `offline-matrix.json`：空 HMAC 与合成 HMAC 的离线对照。
- `channels-api.js`、`channels-comments.md`、`channels-api.md`：视频号客户端接口参考。
- `wechat-automation.py`、`wechat-capture.py`：公众号客户端自动化及会话捕获参考。

实验源码保存在忽略目录，不属于生产依赖。当前结果不足以新增宣称可用的 App 采集器。

## 原始输入续测：传输、浏览器与计数归属

以下是同日后续验证，不能与前面的 30 条原始样例结果重复累计。仍使用原始 JSON 的 `url + media_name`，未要求调用方补 token、Cookie 或修改链接。文件共有小红书 589 条、微信 1513 条、微信视频号 208 条；源文件 SHA256 保持为 `6d623105e9da6065b4c2f57faa5fe2b5f7fc8d5a4f9b0be9b0f09e670fa83bec`。

### 已完成的代码修复

- **小红书计数必须属于目标笔记。** 原实现用目标 ID 后一段文本中的第一组 `interactInfo`，当目标没有统计而相邻推荐笔记有统计时会串数据。现在解析 `noteDetailMap`，同时校验 map key 和 `note.noteId`，只读取目标的计数；兼容状态中的裸 `undefined`，保留字符串内容及真实零值。
- **代理 CONNECT 恢复扩展到实测主机。** 同一代理连接 `res.wx.qq.com`、`webapi.rednote.com` 的域名收到 CONNECT 403，使用 DNS 公网 IPv4 后能够建立 CONNECT 和 TLS；因此加入受限恢复名单。TLS 证书校验、SNI、目标域名、代理出口保持不变。源站拒绝、其他主机、私网解析及非 443 端口不会触发恢复。
- **公众号浏览器静态资源恢复。** `wechat_browser_resources.py` 只代理加载官方 `res.wx.qq.com` 的脚本、样式、图片、字体，复用当前浏览器代理，不转发 Cookie 或 Authorization；并发为 2、单次 10 秒、响应体接受上限 2 MiB。导航、文章 API 与验证接口仍由浏览器处理。恢复路径接入 `BrowserFallback`，但互动采集器原有的公众号浏览器禁用策略没有放开，不能据此宣称线上公众号接口已新增成功路径。

### 实际平台结果

| 路径 | 实测结果 | 可得结论 |
| --- | --- | --- |
| 小红书全新游客激活、身份查询、搜索目标原始 ID | 激活和 `user/me` 成功；搜索返回 `code=-104`，无目标结果 | 访客会话可自动取得；本轮仍无法取得目标详情 token |
| Rednote 域名替代路径 | 桌面浏览器访问目标裸 ID 仍进入 404；激活尝试出现 TLS 错误 | 不构成已验证的替代采集器；没有关闭证书验证 |
| 3 篇原始公众号文章，移动 GET、移动 POST、桌面 POST | GET 进入环境验证；POST 返回 `ret=-3`，无互动字段 | 旧项目的 `is_only_read` POST 方法在本次样例中没有恢复数据 |
| 公众号代理浏览器 | 能恢复之前失败的官方静态脚本；其他验证请求仍可能超时 | 修复了空白页的一项原因，但不等于获得文章会话 |
| 原始第 1 篇公众号，独立直连浏览器诊断 | 一次全新无账号会话约 15 秒后自动通过验证，取得正确标题、正文和 `comment_id`；互动字段仍为空 | 本次取得了原始文章正文，尚未取得互动计数；直连仅用于独立诊断，生产配置不变 |
| 随后 3 篇公众号的全新浏览器复测 | 三篇均停在环境验证，未重现正文成功 | 上述正文成功不能表述为稳定可用 |
| 用真实 `comment_id` 调公众号评论接口 | 代理/直连 × 最小参数/页面参数共 4 次，均 HTTP 200、`ret=-3 / no session` | 仅有正文参数未形成有效评论会话；未返回评论及总数 |
| 视频号桌面详情入口 | 独立无账号请求返回 `errCode=10012 / Illegal request` | 普通网页请求未形成客户端详情上下文 |
| 视频号原始 export ID 的预览与原生详情协议探测 | 当前代理请求超时或连接失败；原生 JSON 请求未复现微信二进制传输 | 不能把这次网络失败当作平台业务鉴权结论 |
| 元宝链接解析：原始 URL 和可用 sph 对照 | 两条均 HTTP 401 | 未取得该服务会话，不能作为本轮自动解析来源 |

另尝试公开 Reader 服务读取三个原始链接，均在连接阶段超时，没有取得内容；这一结果不能用于判定平台是否允许读取。

### 回归结果与发布状态

候选代码通过真实 FastAPI 路由复测两条文件外对照：小红书返回点赞 **3657**、评论 **2162**、分享 **6907**、收藏 **725**；视频号返回点赞 **54000**、评论 **4214**、分享 **39000**、收藏 **100000**。视频号万/+格式来自展示计数换算，不能作为精确后台总数。两条均 HTTP 200，不能计入原始样例成功率。

同一候选接口各测一条原始样例，三个平台均 HTTP 502，未返回互动字段。原始数据采集的业务验收仍未通过。

- 完整回归：**368 passed、22 skipped**。
- 使用项目声明的 `curl_cffi==0.9.0` 独立解压依赖运行 HTTP、游客会话及静态资源相关测试：**51 passed**，未替换当前环境的依赖。
- 本次实验沿用 10 核、5,000,000,000 字节内存、禁用 swap 的 cgroup；一次完整浏览器复测观测峰值为 669,208,576 字节。
- 本次修改保留在工作区，8200 未重启，仍为此前的 URL 兼容版本；未放开公众号耗时浏览器重试。

续测证据目录：`.local/analysis/three-platform-continued-20260920/`。`verification-results.json` 为候选 API 结果，`protocol-results.json` 为原始协议探测，`connect-results.json` 为传输定位，`browser-click-results.json` 为取得公众号正文的浏览器记录，`wechat-context-results.json` 为未能复现的后续浏览器结果，`wechat-comment-protocol-results.json` 为四次评论请求结果，`channels-detail-results.json` 为视频号与元宝结果。原始页面与会话材料保存在忽略目录的受限权限文件中。

### 本轮核对的参考实现

- [openweb 小红书适配说明](https://github.com/imoonkey/openweb/blob/main/src/sites/xiaohongshu/DOC.md) 与源码：使用搜索/推荐流取得笔记 token，再读详情；“无需登录”的说明不能代替全新会话实测。
- [tamnd/xiaohongshu-cli](https://github.com/tamnd/xiaohongshu-cli)：核对笔记读取源码后，详情仍依赖笔记 token，没有可直接套用的裸 ID 读取成功证据。
- [Redbook](https://github.com/lucasygu/redbook)：可参考 Rednote 域名、签名与 Cookie 隔离，已实际探测替代域名。
- [微信公众号接口旧实现](https://github.com/wnma3mz/wechat_articles_spider/blob/master/docs/使用的微信公众号接口.md)：借鉴 POST 取评论参数及评论请求定义；本次真实样例返回 `no session`。
- [wx_channel 详情适配](https://github.com/nobiyou/wx_channel/blob/master/internal/assets/inject/api_client.js) 与 `utils.js`：`encrypted_objectid` 对应客户端详情参数；`WXU.API` 来自微信页面加载的 API 对象，源码本身不提供无账号的客户端会话初始化。
- [wx_channels_download 作者讨论](https://github.com/ltaoo/wx_channels_download/discussions/250)：提供元宝解析与公开预览调用线索；本轮没有使用讨论中的设备、账号或 token 值。

## App 与网页联合续测：新版公众号计数与 Android 启动

本节记录上述续测之后的工作。原始输入仍为同一份 JSON，没有修改 URL 或要求调用方补会话。新增的是公众号字段解析修复；尚未新增通过原始样例验收的采集路径。

### 公众号新版页脚

核对此前取得的原始公众号页面及其官方脚本，页面通过 `window.appmsg_bar_data` 下发新版页脚状态。根据脚本使用的按钮标签与计数绑定，新增以下映射：

| 平台字段 | 页面含义 | API 字段 |
| --- | --- | --- |
| `read_num` | 阅读 | `views` |
| `old_like_count` | 赞 | `likes` |
| `like_count` | 推荐 | `recommendations` |
| `comment_count` | 留言 | `comments` |
| `share_count` | 分享 | `shares` |
| `collect_count` | 收藏 | `favorites` |

`red_flower_like_info.is_red_flower_like=1` 时，该按钮显示为“助力”，不能把其计数当作推荐。解析同时支持 HTML 内的页脚对象与 JSON 响应的同名对象；只有明确提供的数值才合并覆盖已有统计，保留旧格式可用字段和真实零值。

真实页面中存在 `old_like_count: '' * 1` 等模板占位。虽然 JavaScript 会把这个表达式计算为 0，采集器仍将其视为未提供数值。重新解析此前取得的原始文章，结果仍没有互动字段，未把占位转成成功结果。

新增四项测试覆盖新版字段区分、空字符串、部分统计合并与真实零值、特殊助力按钮。完整回归为 **372 passed、22 skipped**。

### 小红书官方 App 动态环境

继续使用已下载的官方 9.47.0 APK、Android 11 Google APIs x86_64 镜像及镜像内的 ARM 转译库。静态 DEX 检索定位到新版评论与详情路径，例如 `/api/sns/v1/interaction/comment/merged/l1/page`、`/api/sns/v1/note/imagefeed`。这些字符串只提供定位线索，不证明已掌握请求参数、设备初始化或有效签名。

本机没有 `/dev/kvm`，模拟器使用软件执行。此次先成功启动 Android，随后小红书进程反复在约 10 秒后被 ActivityManager 终止，日志为启动后未及时 attach；同时观察到耗时的 DEX 校验。没有取得稳定的 App 游客页面或成功的详情、评论响应。

针对启动原因做了两项独立调整：通过 ADB 实际设置编译过滤参数并验证读回；设置 `ro.hw_timeout_multiplier=20` 后重启 Android 框架，使框架重新加载启动超时配置。这些设置仅作用于隔离的分析模拟器。结束前仍停在 Android 启动画面，尚未验证超时调整能让小红书稳定运行，不能把它记录为成功修复。

参考依据为 [Android 模拟器命令行说明](https://developer.android.com/studio/run/emulator-commandline)、[Android 运行时编译配置](https://source.android.com/docs/core/runtime/configure/package-manager) 及 [AOSP 软件模拟器超时倍数实现](https://android.googlesource.com/platform/frameworks/base/+/0fcb8d6db42f6c20f6b69d47e02c54e2706f88f9%5E2..0fcb8d6db42f6c20f6b69d47e02c54e2706f88f9/)。模拟器 `-prop` 并非可设置任意 Android 属性，后续诊断应验证实际属性值，不能仅以启动参数存在为依据。

分析 unit 运行约 10 分 45 秒后正常清理。仍限制 10 核 CPU、内存不超过 5,000,000,000 字节、swap 为 0；过程观测到内存约 4,002,254,848 字节，systemd 最终报告峰值约 3.7 GiB、swap 峰值 0。未发生 OOM，实验结束后模拟器和专用 ADB 服务已释放。受当前软件模拟速度限制，重复同样的启动实验暂时不能提供新的平台请求证据。

### 本轮真实 API 核验

通过候选代码的 FastAPI 路由，依次验证两条文件外对照和三个平台各一条原始输入：

| 输入 | HTTP | 实际结果 |
| --- | --- | --- |
| 小红书有效 token 对照 | 200 | 赞 3657、评论 2162、分享 6907、收藏 725 |
| 视频号公开 `sph` 对照 | 200 | 赞 54000、评论 4214、分享 39000、收藏 100000；展示数换算仍不是后台精确总数 |
| 原始小红书裸 ID URL | 502 | 无互动字段，详情缺少当前可用 token |
| 原始公众号 URL | 502 | 本次请求失败，无互动字段 |
| 原始视频号 `export/...` URL | 502 | 当前适配器没有可用的微信客户端详情上下文 |

两条对照不能计入原始数据成功率。本轮三条原始输入均未成功，评论获取也没有新增成功证据。后续仍需先取得真实 App 游客详情请求，才能验证设备参数与网页会话能否组合复用；当前无法承诺增加代理即可补齐这些条件。

证据目录为 `.local/analysis/app-web-joint-20260920/`：`android-status.json` 记录启动阶段，`apk-public-endpoints.json` 记录 APK 定位线索，`verification-results.json` 记录候选 API 响应；原始日志受限权限保存。修改保留在工作区，8200 未重启，结束检查健康接口仍为 `status=ok`。

## 游客协议与公众号合集续测

本次续测没有修改生产采集代码，没有新增通过原始 URL 互动验收的路径。实验与请求证据位于 `.local/analysis/guest-request-20260920/`，原始响应使用 0600 权限保存。上轮完整回归的 **372 passed、22 skipped** 是此前结果，本轮只改分析脚本与文档，未重复运行该回归。

### 修正 Android 超时假设

进一步核对 [AOSP Android 11 的 ActivityManagerService](https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android11-release/services/core/java/com/android/server/am/ActivityManagerService.java)：该分支的 `PROC_START_TIMEOUT` 是整型常量 `10*1000`。因此上一轮设置 `ro.hw_timeout_multiplier` 的值，不能证明这份镜像中的启动等待时长已改变；较新分支的实现不能直接套到 Android 11。

此次尝试在隔离模拟器的 `ActivityManagerService.processStartTimedOutLocked` 中，只为 `com.xingin.xhs` 延后一次启动超时，避免重启整个框架，也避免只修改被编译内联的常量。Android 约 210 秒启动完成，Frida 服务进程存在，但主机连接调试服务时出现 `frida.TransportError: timeout was reached`。Hook 未成功安装，未再次启动 App，没有得到新的 App 请求证据。`runtime-status.json` 记录全过程，`runtime_timeout.js` 是尚未动态验证成功的诊断脚本。

实验约 4 分 52 秒结束并清理；仍由 cgroup 限制 10 核、内存不超过 5,000,000,000 字节、swap 为 0。systemd 报告峰值约 3.7 GiB、swap 峰值 0。之后转为低内存静态和协议分析，没有继续重复整机启动。

### 小红书：从字符串定位到请求参数

逐个解析官方 APK 的 `classes7.dex`、`classes8.dex`、`classes13.dex` 的接口注解，产出 `native-route-contracts.json`。新增准确定位：

- `com.xingin.notebase.notedetail.service.NoteDetailService.queryNoteDetailFeedData3Up`：`/api/sns/v1/note/imagefeed`，11 个参数，包括 `note_id`、`page`、`num`、`fetch_mode`、`source`、`source_scene` 等。
- `com.xingin.matrix.comment.model.service.CommentService.getMergedCommentList`：`/api/sns/v1/interaction/comment/merged/l1/page`，参数为 `note_id`、`type`、`start`、`source`、`page_context`。
- 同时保留备选评论、视频详情、激活接口的注解作为对照。

首次网页游客激活因网络超时未进入内容验证；使用新的代理会话重试后，激活与同一游客身份验证成功。保持同一会话，针对文件中的原始小红书 ID 进行三次兼容性验证：

| 请求 | 结果 |
| --- | --- |
| APK 图片详情路径 + 网页游客签名 | HTTP 406；`success=true, code=0`，无 `data` |
| APK 合并评论路径 + 网页游客签名 | HTTP 406；`success=true, code=0`，无 `data` |
| APK 图片详情路径 + 网页会话及 App 公共请求头 | HTTP 406；`success=true, code=0`，无 `data` |

这是“网页游客能否复用到 App 路径”的兼容性探测，未复现 native 设备初始化、`shield` 和 `x-mini-*`，不能等同于完整 App 请求。三个响应都没有计数或评论，未计作采集成功。证据为 `hybrid-results.json`。

### 公众号：公开合集可读，单篇互动仍缺失

从此前取得的原始第一篇文章中读取其官方合集链接，测试 `mp/appmsgalbum?action=getalbum`。代理请求超时；独立直连诊断的 HTML、JSON 均 HTTP 200，JSON 为 `base_resp.ret=0`，能够核对正确的 `msgid + itemidx`，取得文章标题、发布时间、封面和官方文章 URL。

使用同一无账号 HTTP 会话，先读合集，再访问其返回的文章 URL（含平台提供的 `chksm`），两次实验均取得了正确的原始文章 HTML 和 `comment_id`。这没有单独证明是 `chksm`、合集访问还是当时网络状态促成了成功，尚无充分对照可把某一项定为原因。

- `getappmsgext` 请求返回 `ret=0`，但只有合集、标签、广告等辅助数据，没有 `appmsgstat` 或其他互动计数。
- 按当前官方脚本补齐请求字段，并改用 `is_only_read=0`、`appmsg_like_type=2`、`tmp_version=1`，仍返回辅助数据，没有计数。
- 评论请求仍返回 `ret=-3 / no session`，没有评论正文。
- 合集有自己的 `base_info.read_count`；对应的单篇文章 `read_count`、`elected_comment_count` 未提供。合集总数不能代替单篇文章的计数，即使当前合集只有一篇文章。

随后从原始 JSON 按不同 `__biz` 选取 8 篇（数组下标 41、60、88、90、98、101、104、154）：8 次扩展信息接口均 `ret=0`；其中 3 篇可以通过其返回的合集准确找到目标文章，但目标条目均没有互动字段。**本次 8 篇新增诊断的互动成功数为 0**；有文章列表或辅助信息不等于互动接口成功。证据为 `album-results.json`、`album-context-results.json`、`wechat-batch-results.json`。

核对了微信官方当前 `album`、`appmsgext`、`appmsg_comment`、`page_utils` 脚本，下载地址及 SHA256 保存于 `references.json`。公众号脚本的 `useWebTransferInH5` 分支调用微信桥接传输；它并不是一个单独的免账号 HTTP 登录接口。其他参考包括 [公开合集列表实现](https://github.com/SlowGrowth1314/opencli-weixin-album) 和 [视频号元宝适配源码](https://github.com/ltaoo/wx_channels_download/blob/main/internal/api/sph.go)；后者仍需要配置有效 Cookie，本轮没有复用示例中的账号或设备值，也没有新增视频号成功证据。

### 可复现实验

在项目目录使用现有 Python 环境运行，所有脚本串行使用同一个资源受限 unit：

```bash
/home/txy/miniconda3/envs/MyAgent/bin/python -m app.manually_execute_script.run_limited_analysis --timeout 240 -- /home/txy/miniconda3/envs/MyAgent/bin/python .local/analysis/guest-request-20260920/inspect_routes.py
/home/txy/miniconda3/envs/MyAgent/bin/python -m app.manually_execute_script.run_limited_analysis --timeout 160 -- /home/txy/miniconda3/envs/MyAgent/bin/python .local/analysis/guest-request-20260920/probe_hybrid.py
/home/txy/miniconda3/envs/MyAgent/bin/python -m app.manually_execute_script.run_limited_analysis --timeout 285 -- /home/txy/miniconda3/envs/MyAgent/bin/python .local/analysis/guest-request-20260920/probe_wechat_albums_batch.py
```

这些脚本用于诊断，不是已验证的生产适配器。下一步应以取得 native 游客设备初始化的成功响应、或公众号 H5 评论计数的可复现读取链路为准；现有证据不足以把无数据响应包装成成功能力。8200 未重启，原始 JSON 未修改，结束健康检查仍为 `status=ok`。
