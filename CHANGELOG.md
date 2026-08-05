# Changelog

本插件所有版本更新记录。版本遵循语义化版本（`vMAJOR.MINOR.PATCH`）。

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
