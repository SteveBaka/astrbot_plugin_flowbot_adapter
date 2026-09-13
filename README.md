# astrbot_plugin_flowbot_adapter

FlowBot 平台适配器：通过 **FlowBot Docker WebUI 统一端口**连接微信聊天。

请搭配[FlowBot](https://github.com/SteveBaka/FlowBot)使用，如果喜欢的话请点一个 Star，谢谢。

## 架构

```
微信 ←→ FlowBot (Docker) ←→ 插件 API 端口(7400) ←→ 本适配器 ←→ AstrBot
                                          │ WS /api/v1/ws/messages  入站消息
                                          │ HTTP /api/v1/messages/send  出站消息
```

- **入站**：WebSocket `ws://<host>:<port>/api/v1/ws/messages?token=<api_key>`（归一化消息推送）
- **出站**：HTTP `POST http://<host>:<port>/api/v1/messages/send`（Bearer API Key 认证）
- 端口 `7400` 为 FlowBot 插件 API；`7300` 是 WebUI（需登录，API Key 无效）
- 依赖 FlowBot Docker 项目的 **unified-port-message-api**（Unified WebUI 端口扩展），需先确认该扩展已在宿主机构建并启用。

## 配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `flowbot_host` | string | 空 | FlowBot WebUI 主机地址（FlowBot 容器 IP）|
| `flowbot_port` | int | `7400` | FlowBot 插件 API 端口（注意：7300 是 WebUI 需登录，API Key 无效）|
| `flowbot_api_key` | string | 空 | FlowBot Bot Token（WebUI 中插件模式 Bot 配置的 Token，创建 Bot 时自动生成；与 WebUI 登录密码无关）|
| `flowbot_reconnect_interval` | int | `5` | 断线重连初始间隔（秒），指数退避，上限 60s |
| `flowbot_reconnect_max_attempts` | int | `5` | 断线重连最大次数，超过即停止（避免影响性能与日志）；填 0 表示不限制 |
| `flowbot_use_direct_url` | bool | `false` | 允许透传图片 URL 而非下载 base64（省流量），发送失败自动回退 base64 |
| `flowbot_image_size_threshold` | int | `5` | 图片 base64 阈值（MB），超过则透传 URL 或尝试上传 token；避免微信粘贴大图冻结 |
| `flowbot_video_size_threshold` | int | `14` | 出站视频 base64 阈值（MB），超过走 `video_url` 直链由 FlowBot 下载 |
| `flowbot_video_use_direct_url` | bool | `true` | 与图片相反：视频默认走直链（大、更稳），失败回退 base64 |
| `flowbot_text_split_enabled` | bool | `true` | 正文中的空行（`\n\n`）视为多条消息分隔符，拆成多次微信消息依次发送（@/引用仅挂第一条）|

## 入站媒体与引用回复（需 FlowBot ≥ 1.5.x 契约，开关默认关）

| 能力 | 开关（FlowBot WebUI「消息管理」） | 适配器行为 |
|------|------|------|
| 入站视频 | `inboundVideoPushEnabled` | `video_url` 下载 → `Video` 组件；文件缺失时降级封面图 + 文本标注 |
| 入站语音 | `inboundVoicePushEnabled` | `voice_url` 下载 WAV → `Record` 组件；转写/ASR 由 astrbot 配置自理 |
| 引用回复 | 始终可用 | 引用 bot 消息无 @ 直接唤醒（`Reply.sender_id == self_id`）；引用他人透传不唤醒 |

开关关闭时对应事件退化为纯文本占位（`[视频]` / `[语音]`），与旧版行为逐字节一致。

## 安装

1. 在 AstrBot 中安装本插件目录（适配器类型）。
2. 填写上述配置，`flowbot_host` 填能访问到 FlowBot Docker 宿主机的 IP（若 AstrBot 与 FlowBot 同机，可填 `127.0.0.1`），`flowbot_port` 填 7400。
3. 重载插件后，控制台应显示 `FlowBot WS 已连接`。

## 第三方插件调用接口（能力钩子层）

插件经 `context.get_platform_inst("flowbot_adapter")` 获取平台实例后，可直接调用：

| 类别 | 方法 |
|------|------|
| 群查询 | `get_group_list()` / `get_group_info(group_id)` / `get_group_avatar(group_id)` |
| 成员查询 | `get_member_list(group_id)` / `get_member_info(group_id, user_id)` / `get_member_avatar_url(group_id, user_id)` |
| 发送 | `send_text(session_id, content, at_users=None, reply_to=None)` / `send_image(session_id, image)` / `send_message_chain(session_id, chain)` |
| 标准 | `send_by_session` / `send_proactive_by_session` / `get_client` / `get_self_id` / `get_bot_wxid()` |
| 能力 | `capabilities()` |

示例：
```python
from astrbot.api.message_components import MessageChain, Plain

platform = context.get_platform_inst("flowbot_adapter")
await platform.send_text("25543334968@chatroom", "群分析报告已生成", at_users=["all"])
groups = await platform.get_group_list()
avatar = await platform.get_member_avatar_url(groups[0].group_id, "wxid_xxx")
```

## 消息能力

- 文本、图片收发（消息时间戳透传 flowbot 推送的真实发送时间）
- 视频收发（出站三通道：本地文件 upload token / 直链 / base64；入站 token 直链下载）
- 入站语音（`Record` 组件，WAV 由 FlowBot 解码交付；转写/ASR 由 astrbot 配置自理）
- 引用回复：引用 bot 消息无 @ 直接唤醒，引用内容渲染为 `[Quote(昵称: 原文)]` 进模型上下文
- 群聊 / 私聊（会话类型判断，群回复目标为群会话）
- 群 @ 发送（`at_users`，支持 `FlowBotMention(wxid=...)` 自研组件与 AstrBot 内置 `At(qq=...)` 兼容；`"all"` = @全体）
- 回复消息（`reply_to`，需对端支持）
- 群/成员查询（第三方插件可用）：`get_group_list` / `get_group_info` / `get_member_list` / `get_member_info`（懒加载 + 60s 缓存）
- 头像：入站推送 `avatar_url` 随 `raw_message` 透传；`get_group_avatar` / `get_member_avatar_url` 查询群/成员头像
- 文件发送：flowbot Linux 容器不支持，`File` 组件记 warning 降级日志

## 分段发送

- 正文中的空行（两个及以上换行 `\n\n`）视为多条消息分隔符，拆成多次微信消息依次发送；单个换行保留为段内换行
- `at_users` 与引用（`reply_to`）仅挂在第一条分段上，后续分段为纯文本
- 兜底 outputpro 等分段插件（其 `SplitStep` 平台白名单不含本适配器，不会对 FlowBot 平台拆分）；若上游已拆段发送，到适配器的每条消息天然无空行，本逻辑不会重复触发
- 开关 `flowbot_text_split_enabled`（默认开）

## 机器人身份（self_id 链路）

- 适配器维护机器人真实 wxid：启动时从 FlowBot `GET /api/v1/bot/self`（Bot Token 鉴权，`source=config` 权威 / `learned` 服务端学习兜底）预热，运行期从每条推送的 `self_id` 学习，落盘 `<AstrBot data>/flowbot_adapter_bot_wxid` 跨重启生效；预热失败自动回退推送学习（INFO，不报错）
- 每条入站事件 `abm.self_id` 均为真实 wxid → `event.get_self_id()` 全链路正确（@ 唤醒、引用 is_self 唤醒、记忆/分析类插件的身份判定都依赖它）
- `get_bot_wxid()`：能力钩子，供插件在非事件上下文（定时任务/proactive/Web）查询真实 wxid；`get_self_id()` 未学到 wxid 时回退 meta id
- 自发消息（回显）过滤：`sender_id == self_id` 确定性丢弃（覆盖图片/视频等一切回显）；仅当旧版 FlowBot 不带 `self_id` 时才回退 3 秒内容匹配，真人复读不再被误丢

## 图片发送策略
- 本机文件存在 → `image_base64`（读文件）+ `image_path`（同主机兼容）
- `base64://` / `data:` / **裸 base64** URI 源（如 T2I output_pro 产物）→ 直接提取 base64 串进 `image_base64`，不落盘不二次下载
- `file:///` 形态 → 剥前缀后按本地文件处理
- URL 源且 `flowbot_use_direct_url=true` → 直接透传 `image_url`（省流量），同时预下载一份本地缓存；若透传失败且有本地文件，自动回退以 `image_base64` 重发
- URL 源默认（透传关闭）→ 下载后以 `image_base64` 发送
- 文件超过 `flowbot_image_size_threshold`（MB，默认 5）→ **直接跳过**（flowbot 硬上限 5MB，>5MB 所有来源拒绝）
- 入站图片源归一化：FlowBot 推送的 `image_base64` / `base64://` / `data:` / 裸 base64 / URL / 本地路径均转成规范 `Image` 组件；base64 形态不落盘
- 临时文件上限 100 个，超限自动清理最旧，防空间膨胀
- 图片下载超时 15s，上限 5MB，失败记日志并跳过
- WebSocket 心跳保活 30s，及时检测断线
- FlowBot 图片硬上限 5MB（体积 >5MB 或宽/高 >4096px 拒绝；粘贴熔断 30s）

## 注意事项

1. **网络链路**：AstrBot 所在机器必须能访问 FlowBot 插件 API 端口（Docker 需 `-p 7400:7400` 映射；若同时用 WebUI 再映射 7300）。
2. **图片回显**：发送图片后，若对端推送的回显消息与发送内容一致，已通过短时去重抑制 ping-pong。
3. **API Key 认证**：插件 API 认证使用 FlowBot WebUI 中 Bot 配置的 Token（创建 Bot 时自动生成，非 WebUI 登录密码，容器也无 `weflow_webui_api_key` 环境变量）。适配器 `flowbot_api_key` 需填写同一 Token，不匹配或为空时 HTTP/WS 均返回 401。
4. **消息去重**：内置 10 分钟 message_id 去重，防止重复回调。
5. **跨主机图片**：AstrBot 与 FlowBot 分机部署时，本机临时文件路径对 FlowBot 不可见，依赖 base64/URL 传输。
6. **长期实测效果**：由于微信部分字段和接口不一致，所以部分采用的是自研方案，部分插件会存在兼容性不佳的问题，尽请谅解 （。
## 开发调试

```bash
# 仅验证语法
python -m py_compile main.py
```

