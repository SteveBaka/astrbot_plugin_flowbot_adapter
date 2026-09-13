# Changelog

本插件所有版本更新记录。版本遵循语义化版本（`vMAJOR.MINOR.PATCH`）。

## v1.4.2 — 2026-09-14

### 修复：wxid 预热路由
- 预热由 `GET /api/v1/mgmt/config` 切换为 `GET /api/v1/bot/self`（FlowBot
  v1.5.x 新增，Bot Token 鉴权）：插件 API 代理本就排除 mgmt（最小权限），
  旧路径恒 404。`source=config` 为权威值，`learned` 为服务端身份库兜底，
  `none`/请求失败回退首条推送学习。
- 预热失败（404/网络错误）降级为 INFO 日志，不再走通用 ERROR 路径。
- 预热改为先于 WS 连接执行，保证 wxid 缓存在首条事件前就绪。

## v1.5.0 — 2026-09-14

### 新增：机器人身份链路（self_id 端到端）
- 启动预热：从 FlowBot `GET /api/v1/mgmt/config` 白名单键 `myWxid` 主动获取
  登录 wxid，消除重启后到首条推送之间的身份盲区（该窗口内 @ 唤醒与引用
  is_self 判定失效）。
- 运行期学习 + 落盘：推送 `self_id` 变化即更新缓存并写入
  `<AstrBot data>/flowbot_adapter_bot_wxid`，跨重启即时生效。
- `get_self_id()` 改为优先返回真实 wxid（此前返回 meta id，第三方插件在
  非事件上下文查询到的是假身份）；新增能力钩子 `get_bot_wxid()` 供插件
  显式查询。
- 自发消息（回显）过滤升级：`sender_id == self_id` 确定性丢弃，覆盖图片/
  视频回显（此前媒体发送登记空内容、回显必然穿透内容匹配）；内容匹配
  降级为旧版服务端（无 `self_id` 字段）专用兜底，修复真人 3 秒内复读
  机器人消息被误丢的问题。

## v1.4.1 — 2026-09-05

### 新增：出站文本空行分段
- `_send_to_session` 将正文中的空行（`\n\n`）视为多条消息分隔符，拆成多次
  FlowBot 发送依次落为多个微信气泡；`at_users` 与引用仅挂第一条分段。
- 背景：outputpro 等分段插件的 `SplitStep` 平台白名单不含本适配器，其分段
  逻辑对 FlowBot 平台直接短路，LLM 多段回复会合并为单条；本逻辑在适配器侧
  兜底拆分。上游已拆段时每条消息无空行，不会重复触发。
- 开关：`flowbot_text_split_enabled`（默认开）。

## v1.4.0 — 2026-09-04

> 合并 v1.4.0/v1.4.1/v1.5.0 三轮未发布的开发迭代，对齐 FlowBot
> `docs/dev/ADAPTER-MEDIA-CONTRACT.md`（2026-09-03/04 版契约）。

### 新增：入站视频推送
- `convert_message` 新增 `type == 'video'` 分支（`_normalize_inbound_video`），
  按契约行为矩阵三档降级：视频本体（`video_url` 下载 → `Video` 组件，封面挂
  `cover`）/ 封面降级段（`fileMissing` 时封面图 + 文本标注）/ `[视频]` 文本占位。
- 流式落盘下载（上限 100MB 对齐 `videoMaxBytes`，120s 超时），独立临时文件池
  （10 个上限 + 30 分钟 TTL），不占用图片池。
- FlowBot 侧开关：`inboundVideoPushEnabled`（默认关，关闭时零影响）。

### 新增：入站语音推送
- `convert_message` 新增 `type == 'voice'` 分支（`_normalize_inbound_voice`），
  两档行为：WAV 下载（HEAD 预检体积 + 10MB 闸 + 60s 超时）→ `Record` 组件
  （转写/ASR 由 astrbot 管线自理）/ 带时长的文本占位 `[语音(Ns)]`。
- FlowBot 侧交付 WAV（24kHz/16bit/mono，silk 解码内置，零转写），token 直链
  TTL 1h 且支持 Range/HEAD。FlowBot 侧开关：`inboundVoicePushEnabled`（默认关）。

### 新增：引用回复唤醒
- 推送携带 `quoted_*` 五字段时构造 AstrBot `Reply` 组件（components[0]）：
  `quoted_is_self=true` 时 `sender_id` 钉死为 `self_id`——群聊引用 bot 消息
  无需 @ 直接唤醒（`Reply.sender_id == self_id` 等式）；引用他人原样透传不唤醒。
  LLM 上下文经 astrbot 内置渲染为 `[Quote(昵称: 原文)]`。

### 修复
- **多段文本粘连**：`_send_to_session` 中多个 `Plain` 组件由无分隔 `"".join`
  改为 `"\n".join`——上游分段插件产出的多段文本不再被粘连成无换行长串。
- **Reply.qq pydantic 校验崩溃**：astrbot v4.27.4 的 `Reply.qq` 为
  `int | None`，传 wxid 字符串会导致整条消息被丢弃；移除该 deprecated 字段
  传参，并为 `Reply` 构造增加异常防御（构造失败降级为无引用文本，不丢消息）。

### 变更
- 配置说明修正：`flowbot_api_key` 实为 FlowBot WebUI 中插件模式 Bot 配置的
  Token（创建 Bot 时自动生成），与 WebUI 登录密码无关；容器无
  `weflow_webui_api_key` 环境变量（历史文档笔误）。
- `handle_msg` 事件周期临时文件登记从仅 `Image` 扩展到 `Image` + `Record`。
- 依赖：入站视频/语音需 FlowBot ≥ 1.5.x 契约（`ADAPTER-MEDIA-CONTRACT.md`），
  开关默认关；旧版 FlowBot 或开关关闭时行为与 v1.3.x 逐字节一致（向后兼容）。

## v1.3.1 — 2026-08-22

### 新增（视频链路统一：upload token / media_path 第三分支）
- **`_send_video` 三分支重构**：
  1. **本地文件**（`os.path.isfile`）→ `_upload_media(kind="video")` 上传 → `media_path` 引用 FlowBot 落盘产物（`/tmp/weflow_uploads/<token><ext>`）。
  2. **http(s) 直链** → `video_url`（FlowBot 自行下载）。
  3. **base64 源**（`base64://`/`data:`/裸 base64）→ ≤ `flowbot_video_size_threshold`(14MB) → `video_base64`，否则跳过。
- 修复此前「本地大文件（如 44MB）无 http 直链又被 14MB base64 阈值拒绝 → 直接跳过」的链路缺口：现本地文件统一走上传通道。
- **`_upload_media` 扩展**：支持 `kind`（`image`/`video`）、`source_url`（`<kind>_url` 提交），返回 `{"token","path"}` dict（此前仅返回 str token）。

### 已知遗留
- 超 FlowBot upload body 上限（20MB）的本地文件上传仍会失败（如 44.71MB 视频），告警记录、待 FlowBot 侧 upload 通道扩容后打通。

## v1.3.0 — 2026-08-21

### 新增（视频发送支持，对齐 FlowBot VIDEO-SEND-DESIGN §六）
- **`_send_video`**：新增视频/文件发送方法，`POST /api/v1/messages/send type=video`。≤ `flowbot_video_size_threshold`（默认14MB）走 `video_base64` 直传，超过走 `video_url` 直链由 FlowBot 下载；支持本地文件（`os.path.isfile` 校验后读文件转 base64）、`base64://`/`data:`/裸 base64、http(s) 直链三类来源。与 `_send_image` 同构，全函数 try/except（旧版 FlowBot 400 → 告警日志，不炸消息循环）。
- **`_send_to_session` 分发**：`Video` 组件自动发出；`File` 组件由原"日志丢弃"升级为复用视频通道（微信端同为粘贴）。
- **能力声明**：`capabilities()` 的 `supports_video`/`supports_file` 置 `True`；`supports_voice` 保持 `False`。
- **第三方钩子**：新增 `send_video(session_id, video)`（对齐 `send_text`/`send_image` 暴露模式）。
- **helper**：新增 `_extract_base64(source)` 与 `_normalize_video_url(url)`（复用 `_fix_image_url` 容器地址改写）。
- **新配置**：`flowbot_video_size_threshold`(14MB) / `flowbot_video_use_direct_url`(true)。

### 说明
- 双端设计参考：`docs/dev/ADAPTER-VIDEO-UPDATE.md`（适配器侧）与 FlowBot `docs/dev/VIDEO-SEND-DESIGN.md` §五.7、§六。
- v1.3.1 起已实现 `media_path`/upload token 分支（本地文件统一走上传通道，见 v1.3.1 条目）；`Record`（语音）仍不支持。
- 平台适配器升级：需完全重启 AstrBot 使新 FlowBot 连接实例生效（reload 不替换运行中实例）。

## v1.2.7 — 2026-08-19

### 修复（成员缓存：头像查询死代码 + 缓存通道失效）
- **`get_member_avatar_url` 缓存失效死代码**：原实现 `getattr(cached[1], "_raw", None)` 中的 `_raw` 从未被写入（缓存值是无该属性的 `MessageMember` 列表），导致**每次头像查询都绕过 60s 缓存重复请求 `/api/v1/group-members`**。现改为在 `get_member_list` 落缓存时同步保留原始 member dict（`group_id -> (fetched_at, members, raw_items)`），`get_member_avatar_url` 复用同一份请求结果命中 `avatarUrl`，命中 TTL 内不再重复请求。
- **上游失败不污染缓存**：`Get /group-members` 失败（`_api_json` 返回 `None`，如 401/4xx/5xx/网络异常）时不再写入空缓存，回退返回过期的已缓存成员，避免一次短暂接口故障导致整组成员"无昵称/无头像"长达 60s（该故障在头像查询真正服用缓存后才可被观察到）。
- **wxid 匹配归一化**：`get_member_avatar_url`/`get_member_info` 改用 `_wxid_match`（剥离 `wxid_` 前缀 + 小写比较）匹配成员。FlowBot `group-members` 返回的 `wxid` 恒带 `wxid_` 前缀（如 `wxid_kbwhoagqmspj21`），而消息侧 `sender_id` 可能带/去前缀，严格相等会导致头像/昵称匹配失败、退化为显示原始 wxid。

### 新增
- **`force_refresh` 逃生通道**：`get_member_list(group_id, force_refresh=True)` 与 `get_member_avatar_url(..., force_refresh=True)` 可强制绕过 TTL 刷新，供调用方（如日报插件）在成员缺失昵称/头像时自救。

### 说明
- 契约不变：昵称仍优先 `displayName`/`groupNickname`/`nickname`，头像取 `avatarUrl`，二者均按 `wxid` 对齐；`get_member_info`/`get_member_list` 签名新增可选参数，向后兼容。

## v1.2.6 — 2026-08-14

### 变更（对齐 flowbot 5MB 硬上限）
- **`_MAX_DOWNLOAD_BYTES`** 10 → **5MB**（flowbot 所有图片来源统一 5MB 限制，不再下载 >5MB 图）
- **`capabilities()["image_max_bytes"]`** 10 → **5MB**
- **`_send_image` 路由简化**：base64 源与本地文件 >5MB **直接跳过**（不再尝试 media/upload token 或 URL 透传——flowbot 已拒）；≤5MB 一律 base64 直发
- `_upload_media` 保留（供未来/大图场景），但当前发送路径不再调用
- use_direct_url 的 URL 透传保留（适配器无法本地探测 URL 图片大小，由 flowbot 侧校验拒绝）

## v1.2.5 — 2026-08-14

### 变更（图片大小阈值整体下调，防微信粘贴大图冻结）
- **base64 直发阈值**：`flowbot_image_size_threshold` 默认 10 → **5MB**（≤5MB 走 base64 直发，微信粘贴流畅）
- **下载硬上限**：`_MAX_DOWNLOAD_BYTES` 20 → **10MB**（>10MB 不再下载，避免大图处理卡顿）
- **能力声明**：`capabilities()["image_max_bytes"]` 同步为 10MB
- 发送路由：≤5MB base64 直发 → 5-10MB 有 URL 透传 / 无 URL 走 media/upload token（flowbot 上限 20MB，上传通道不走微信粘贴，安全）→ >10MB 拦截下载

## v1.2.4 — 2026-08-14

### 变更
- **适配 flowbot 推送契约 v1**（feat/session-avatar-enrich，3dff6a81）：
  - sender 昵称兜底链：`sender_name` → `sender_card` → `source_name` → `sender_id`（flowbot 新增 `sender_name`，旧契约兼容）
  - 群消息 `abm.group.group_avatar` 填充 `group_avatar_url`（flowbot 新契约：`avatar_url` 群消息置空、群头像移入 `group_avatar_url`）
  - 新增 `_inbound_group_avatars` 缓存：入站 `group_avatar_url` 实时缓存，`get_group_avatar()` 优先取用，sessions 兜底

### 说明
- `avatar_url` 语义已按上游契约改为"发送者头像"；群消息该字段为空，发送者头像由 `get_member_avatar_url` 经 group-members 查询

## v1.2.3 — 2026-08-13

### 新增
- **第三方插件可调用的能力钩子层**（插件经 `context.get_platform_inst("flowbot_adapter")` 获取实例后直接调用）：
  - `capabilities()`：能力声明（支持 text/image/at/reply/group/proactive/avatar，不支持 file/voice/video，max_text_length=1800）
  - `send_text(session_id, content, at_users=None, reply_to=None)`：便捷文本发送
  - `send_image(session_id, image)`：便捷图片发送（Image 组件或路径/URL/base64 字符串）
  - `send_message_chain(session_id, message_chain)`：便捷消息链发送
  - `get_self_id()`：平台级机器人标识

### 说明
- 完整对外 API 面：查询（`get_group_list/get_group_info/get_group_avatar/get_member_list/get_member_info/get_member_avatar_url`）、发送（`send_text/send_image/send_message_chain/send_by_session/send_proactive_by_session`）、能力（`capabilities`）、标准（`get_client/platform/meta`）

## v1.2.2 — 2026-08-09

### 新增
- **群头像透传**：`get_group_list` 填充 `Group.group_avatar`（flowbot sessions 接口 enrich 的 `avatarUrl`），新增 `get_group_avatar(group_id)`
- **成员头像查询**：新增 `get_member_avatar_url(group_id, user_id)`（`GET /api/v1/group-members` 的 `avatarUrl` 字段，独立缓存通道）
- **File 组件降级日志**：`_send_to_session` 对 `File` 组件记录 warning 日志（flowbot Linux 容器不支持文件发送，避免静默丢弃导致排查困惑）

### 说明
- 入站消息头像：flowbot 推送的 `avatar_url` 已随 `abm.raw_message` 透传（`raw_message` 即推送 data），插件侧 `event.message_obj.raw_message.get("avatar_url")` 可取；群成员头像可经 `get_member_avatar_url` 获取

## v1.2.1 — 2026-08-08

### 新增
- **消息时间戳透传**：`convert_message` 读取 flowbot 推送的 `timestamp`（unix 秒，来源 WCDB createTime）写入 `AstrBotMessage.timestamp`，供时段统计/增量游标使用真实发送时间；缺省回退到达时间
- **群列表/群成员按需查询**（面向第三方插件，懒加载 + 60s TTL 缓存，非轮询）：
  - `get_group_list()` / `get_group_info(group_id)`：`GET /api/v1/sessions` 过滤 `sessionType=="group"`，群名=`displayName`，群标识=`username`
  - `get_member_list(group_id)` / `get_member_info(group_id, user_id)`：`GET /api/v1/group-members`，成员昵称优先 `displayName`/`groupNickname`/`nickname`

### 变更
- `_upload_media` 上传 token 字段按 flowbot 契约确认：`token` 优先（原 `image_token` 在首位）

### 说明
- B1（File 发送）确认 flowbot Linux 容器不支持，不实施；HTML 报告用图片渲染替代

## v1.2.0 — 2026-08-08

### 新增
- **入站图片源归一化**（`_normalize_inbound_image`）：FlowBot 推送的图片现支持 `image_base64`、`base64://`、`data:`、裸 base64、http(s) URL、本地路径等多种形态，统一转成规范的 AstrBot `Image` 组件。base64 形态**不落盘**（直接构造 `Image(file=base64://...)`），仅 URL/路径才下载
- **出站裸 base64 识别**：`_send_image` 新增无前缀裸 base64 识别（`_looks_like_base64` 保守判定：长度、路径/URL 特征排除、字符集校验），第三方插件即使产出无前缀 base64 也能被吃进

### 优化
- **临时文件容量上限**：`_temp_files` 由无序 set 改为 dict（path→mtime），超过 `_MAX_TEMP_FILES`（100）时按最旧清理，防长期运行空间无限膨胀；`_trim_temp_files` 在登记时即触发

### 说明
- base64 仅在无更优来源时作为保底：优先走本地文件 → base64、URL 透传、media 上传 token；新增的裸 base64/入站 base64 识别均是"兜底兼容"而非首选路径

## v1.1.2 — 2026-08-07

### 变更
- **完整适配 AstrBot Platform 标准接口**：补全第三方插件按标准发现/操作本适配器所需的能力
  - 新增 `get_client()`：返回适配器自身（对应基类预留接口，synochat 等适配器即借此被群分析等插件发现）
  - 新增 `platform` 属性：返回 `"flowbot_adapter"`（第三方 `_detect_platform_name` 优先读取）
  - 新增 `send_proactive_by_session`：主动发送消息标准接口（与 `send_by_session` 同实现）
  - `meta().name` 统一为 `"flowbot_adapter"`，各类名称对齐
- 注：上述仅保证本适配器**可被发现**；第三方插件（如群分析）仍需自行注册 `flowbot_adapter` 到其 DDD 适配器工厂才能拉取历史消息

## v1.1.1 — 2026-08-07

### 变更
- **移除会话预载定时轮询**：删除 `_load_sessions_loop`（每 5 分钟 `GET /api/v1/sessions` + 日志）。`_sessions_cache` 现仅由入站消息驱动更新（`_handle_message` 每次消息写入对应会话），减少每 5 分钟一次的 HTTP 请求与日志输出，降低资源占用。`_sessions_cache` 当前无消费点，移除轮询不影响功能

## v1.1.0 — 2026-08-07

### 变更
- **ruff 格式优化**：对 main.py 执行 `ruff format`，解决 23 处超长行（E501），纯格式重排，无逻辑变更；`ruff check` 全部通过

## v1.0.19 — 2026-08-07

### 修复
- **会话缓存无限增长**：`_sessions_cache` 新增容量上限（2000 条），超限时清理最旧条目，统一经 `_cache_session` 写入（入站消息与会话预载两处），防止长期运行内存缓慢增长

## v1.0.18 — 2026-08-07

### 变更
- **移除 v1.0.16 临时调试日志**：`FlowBot 入站` 日志使命完成（已定位 flowbot 侧 at_users 缺失并修复），不再输出，减少日志噪音

## v1.0.17 — 2026-08-07

### 修复
- **入站消息全部转换失败（UnboundLocalError）**：v1.0.16 新增的调试日志在 `text` 赋值前引用该局部变量，Python 作用域规则导致所有入站消息抛 `cannot access local variable 'text'`。将 `text`/`mtype` 赋值提前到日志语句之前
- **API Key 随图片下载泄漏**：`_download_image` 此前复用带 `Authorization: Bearer` 的共享 session，向第三方 URL 下载图片时会泄露 FlowBot API Key。改用独立的无鉴权 `aiohttp.ClientSession`
- **WS 重连配置读取无异常保护**：`delay` 的 `int()` 转换未包 try/except，配置填非法值会导致 WS 循环启动即崩溃。新增 `_config_int` 辅助方法统一安全读取

## v1.0.16 — 2026-08-06

### 变更
- `convert_message` 增加入站调试日志（self_id / at_users / type / content）

## v1.0.15 — 2026-08-06

### 新增
- **@ 唤醒支持**：`convert_message` 读取 flowbot 推送的 `self_id` 与 `at_users`（flowbot 从消息 XML `atuserlist` 提取），为群消息生成 At 组件实现唤醒：
  - `notify@all`/`all` → `AtAll()`（AstrBot 唤醒条件 3）
  - 命中 `self_id` → `At(qq=self_id, name=group_name)`（唤醒条件 2，`At.qq == get_self_id()`）
  - 组件插入消息链首位，回复带 @ 更自然
- 导入 `AtAll`（`astrbot.api.message_components` 通配导出）

## v1.0.14 — 2026-08-06

### 修复
- **群消息缺失 group 信息**：`convert_message` 为群消息设置 `abm.group = Group(group_id=session_id, group_name=...)`，避免 AstrBot 群上下文错误（`AstrBotMessage.group_id` 回退到 sender 导致 self_learning 等日志群 id 显示为发送者）

### 变更
- 导入 `Group`（来自 `astrbot.api.platform`，核心导出）

## v1.0.13 — 2026-08-06

### 新增
- **FlowBotMention 自研 @ 组件**：wxid 语义，type 复用 `ComponentType.At` 兼容 AstrBot 序列化，`toDict()` 把 wxid 映射进 qq 位置。自有插件可用 `FlowBotMention(wxid="k22236", name="Unsuited.")` 构造真正的 wxid @

### 修复
- **群回复目标修正**：`FlowBotMessageEvent.send()` 改用 `get_session_id() or get_sender_id()`，群消息回复发往群会话而非发送者本人
- **空正文 @ 跳过**：纯 @（无正文无图片）不再发送，避免 flowbot 400 Missing content

### 变更
- `_send_to_session` 识别 `FlowBotMention`（wxid 直取）与内置 `At`（wxid → qq → uid 兼容），`qq == "all"` 识别为 @全体

## v1.0.12 — 2026-08-06

### 变更
- **@ 目标读取优先级调整为 wxid → qq → uid**：FlowBot/微信的 @ 目标本质是 `wxid`，AstrBot 的 `At` 组件字段名沿用 OneBot v11 的 `qq`。读取顺序改为优先取 `wxid`（若未来组件携带该字段），其次 `qq`，再兜底旧版 `uid`；`qq == "all"` 识别为 @全体
- 注：AstrBot `At` 组件本身只有 `qq`/`name` 字段，无法直接构造 `At(wxid=...)`，本改动是在适配器读取侧做语义对齐

## v1.0.11 — 2026-08-06

### 修复
- **`At` 组件字段错误导致发送崩溃**：`_send_to_session` 使用 `comp.uid` 读取 @ 目标，但 AstrBot 当前版本的 `At` 组件字段为 `qq`（`name` 为昵称）。改用 `getattr(comp, "qq")` 并兼容旧版 `uid` 兜底。此前切换模型等命令生成 `[At, Plain]` 回复链时，适配器在 `main.py:478` 抛 `AttributeError: 'At' object has no attribute 'uid'`，导致整条消息发送失败

## v1.0.10 — 2026-08-05

### 修复
- **临时文件泄漏**：`_download_image` 创建的临时文件现纳入 `_temp_files` 追踪集合；出站下载的缓存文件随用随清（try/finally），入站下载的临时图片登记到事件（`track_temporary_local_file`）由 AstrBot 清理，`terminate()` 兜底清除全部残留
- **下载无大小限制**：图片下载改为分块读取并限制 20MB 上限，防止恶意 URL 导致 OOM

### 变更
- **WebSocket 心跳**：`ping_interval` 由禁用改为 30s，及时检测半开连接
- **`_redact_host` 更名 `_mask_host`**：原来只剥协议前缀，现真正对主机名打码（如 `host.***:port`），避免日志泄露内网地址

## v1.0.9 — 2026-08-05

### 修复
- **图片来源识别扩展**：`_send_image` 从仅识别 `file` 字段扩展为依次检查 `file`/`url`/`path` 三字段，任一字段含 `base64://`、`data:`、`file:///`、http(s)、本地路径均可识别
- **官方归一化兜底**：三字段均无可用源时，调用 `Image.convert_to_base64()`（AstrBot 统一处理 base64:// / file:/// / http / 纯路径）后再发送
- **调试日志**：兜底前输出 file/url/path 三字段前 40 字符，便于排查 output_pro 等插件产出的具体格式

## v1.0.8 — 2026-08-05

### 新增
- **支持 `base64://` 与 `data:` URI 图片源**：T2I 等插件产出的 `Image.fromBytes/fromBase64`（file 字段形如 `base64://...`）此前会被识别为"无可用图片源"。现直接提取 base64 串进 `image_base64` 发送（不落盘、不二次下载）；超过阈值则改走 `_upload_media` 上传拿 token
- **支持 `file:///` 形态**：`Image.fromFileSystem` 产出的 `file:///绝对路径` 剥前缀后按本地文件处理
- `_upload_media` 扩展签名：支持 `local_path=` 或直接 `b64=` 两种来源

### 修复
- output_pro/T2I 生成图片发送失败的问题（`base64://` URI 未识别）

## v1.0.7 — 2026-08-05

### 新增
- **断线重连次数限制**：新增配置项 `flowbot_reconnect_max_attempts`（默认 5），连续断线重连超过该次数即停止并退出循环，避免无限重试影响性能与日志；连接成功自动重置计数；填 0 表示不限制

## v1.0.6 — 2026-08-05

### 修复
- **图片兜底规则完善**：`use_direct_url` 模式下无条件预下载一份本地缓存（下载失败不阻断 URL 透传，仅记录 debug 日志），供 `image_url` 透传失败时回退 `image_base64` 重发
- 移除 `_send_image` 中因兜底规则而变为死代码的透传分支

### 变更
- `README.md` 同步更新图片发送策略说明

## v1.0.5 — 2026-08-05

### 修复
- **媒体上传协议不匹配**：`_upload_media` 由 multipart/form-data 改为 JSON `{"image_base64": ...}` 提交，与 FlowBot `/api/v1/media/upload`（仅接受 JSON.parse）契约一致；修复大图无 URL 时上传 400 导致图片静默丢失的问题
- **默认端口错配**：`flowbot_port` 默认值 7300 → 7400（7300 是需登录的 WebUI，API Key 无效）；同步更新 hint 与 `__init__` 兜底端口
- **阈值与 body 上限错配**：`flowbot_image_size_threshold` 默认值 5MB → 10MB（FlowBot body 上限已扩容至 20MB，base64 可承载约 15MB 原图），避免 5–15MB 图片过早走上传/URL 路径
- **透传失败无兜底**：`image_url` 发送失败（HTTP 错误/不可达）且本地有文件时，自动回退以 `image_base64` 重发

### 变更
- `README.md` 架构图、配置表、注意事项由 7300 端口改为 7400，新增图片发送策略说明

## v1.0.4 — 2026-08-05

### 新增
- **图片发送策略**：`_send_image` 支持多路径路由
  - 本地文件 → `image_base64` + `image_path`（同主机兼容）
  - URL 源 + `flowbot_use_direct_url=true` → 直接透传 `image_url`（省流量）
  - 超过 `flowbot_image_size_threshold` → 有 URL 透传 URL；无 URL 调 `/api/v1/media/upload` 拿 `image_token`
- **新增配置项**：`flowbot_use_direct_url`（bool，默认 false）、`flowbot_image_size_threshold`（int MB，默认 5）
- `_download_image` 增加单次下载超时 15s

### 修复
- 本地与远端配置字段名统一（见 v1.0.2 的 `flowbot_` 前缀）

## v1.0.3 — 2026-08-05

### 修复
- **跨主机图片发送失败（400）**：AstrBot 与 FlowBot 分机部署时，本机临时文件路径对 FlowBot 不可见（`fs.existsSync` 找不到）。发送时附加 `image_base64`（读取本地文件 base64 编码），同时保留 `image_path` 兼容同主机部署。flowbot 侧无需改动（`prepareImageInput` 已支持 `image_base64`）

## v1.0.2 — 2026-08-05

### 修复
- **适配器配置字段污染 AstrBot 全局平台配置**：AstrBot 将所有适配器的 `config_metadata` 按字段名合并进共享 `platform.items` 字典，导致本插件的 `port`（"FlowBot WebUI 端口"）覆盖内置 `port`（"回调服务器端口"），污染其他所有含 `port` 字段的适配器表单
  - 处理方式：全部配置字段加 `flowbot_` 前缀（`flowbot_host` / `flowbot_port` / `flowbot_api_key` / `flowbot_reconnect_interval`），避免与 AstrBot 内置字段冲突
  - 注：此为插件侧规避；AstrBot 核心侧共享字典按字段名合并的根因待核心修复

### 变更
- `flowbot_host` 移除默认值 `192.168.66.106`，hint 改为"请填写你的 FlowBot 的容器 IP"
- 同步更新 `__init__` 配置读取与 `README.md`

## v1.0.1 — 2026-08-05

### 修复
- **插件加载失败（未通过 Star 注册）**：插件仅通过 `@register_platform_adapter` 注册了平台适配器，缺少 AstrBot 加载所需的 `Star` 主类。新增 `FlowBotAdapterPlugin(Star)` 作为插件入口（`super().__init__(context)`）
  - 适配器装饰器在模块导入时生效注册平台；`Star` 子类是 star_manager 识别插件已加载的必要条件，两者缺一不可

## v1.0.0 — 初始版本

### 新增
- FlowBot 平台适配器（适配器类型）
- **入站**：WebSocket `/api/v1/ws/messages?token=<api_key>` 归一化消息推送
- **出站**：HTTP `POST /api/v1/messages/send`（Bearer API Key 认证）
- 文本、图片收发；群聊/私聊会话类型判断；群 @ 发送；回复消息
- 消息去重（10 分钟 message_id 窗口）、发送回显 ping-pong 抑制
- 断线指数退避重连（上限 60s）
- 会话预载（`/api/v1/sessions` 昵称映射缓存）
