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
| `flowbot_api_key` | string | 空 | FlowBot WebUI API Key（Docker 环境变量 `weflow_webui_api_key` 的值）|
| `flowbot_reconnect_interval` | int | `5` | 断线重连初始间隔（秒），指数退避，上限 60s |
| `flowbot_reconnect_max_attempts` | int | `5` | 断线重连最大次数，超过即停止（避免影响性能与日志）；填 0 表示不限制 |
| `flowbot_use_direct_url` | bool | `false` | 允许透传图片 URL 而非下载 base64（省流量），发送失败自动回退 base64 |
| `flowbot_image_size_threshold` | int | `10` | 图片 base64 阈值（MB），超过则透传 URL 或尝试上传 token |

## 安装

1. 在 AstrBot 中安装本插件目录（适配器类型）。
2. 填写上述配置，`flowbot_host` 填能访问到 FlowBot Docker 宿主机的 IP（若 AstrBot 与 FlowBot 同机，可填 `127.0.0.1`），`flowbot_port` 填 7400。
3. 重载插件后，控制台应显示 `FlowBot WS 已连接`。

## 消息能力

- 文本、图片收发
- 群聊 / 私聊（会话类型判断）
- 群 @ 发送（`at_users`）
- 回复消息（`reply_to`，需对端支持）

## 图片发送策略

- 本机文件存在 → `image_base64`（读文件）+ `image_path`（同主机兼容）
- `base64://` / `data:` URI 源（如 T2I output_pro 产物）→ 直接提取 base64 串进 `image_base64`，不落盘不二次下载
- `file:///` 形态 → 剥前缀后按本地文件处理
- URL 源且 `flowbot_use_direct_url=true` → 直接透传 `image_url`（省流量），同时预下载一份本地缓存；若透传失败且有本地文件，自动回退以 `image_base64` 重发
- URL 源默认（透传关闭）→ 下载后以 `image_base64` 发送
- 文件超过 `flowbot_image_size_threshold`（MB，默认 10）→ 有 URL 则透传 URL；无 URL 则调用 `POST /api/v1/media/upload`（JSON `{"image_base64": ...}`）拿 `image_token`
- 图片下载超时 15s，上限 20MB，失败记日志并跳过
- WebSocket 心跳保活 30s，及时检测断线
- FlowBot body 上限 20MB（base64 约承载 15MB 原图）

## 注意事项

1. **网络链路**：AstrBot 所在机器必须能访问 FlowBot 插件 API 端口（Docker 需 `-p 7400:7400` 映射；若同时用 WebUI 再映射 7300）。
2. **图片回显**：发送图片后，若对端推送的回显消息与发送内容一致，已通过短时去重抑制 ping-pong。
3. **API Key 认证**：Docker 部署时需确保设置 `weflow_webui_api_key`，否则 HTTP/WS 均返回 401。
4. **消息去重**：内置 10 分钟 message_id 去重，防止重复回调。
5. **跨主机图片**：AstrBot 与 FlowBot 分机部署时，本机临时文件路径对 FlowBot 不可见，依赖 base64/URL 传输。

## 开发调试

```bash
# 仅验证语法
python -m py_compile main.py
```

## 后续规划（可选）

- 定期拉取 `/api/v1/sessions` 建立 昵称 → wxid 映射缓存，用于更友好的群成员展示
- 对接 OneBot v11 协议（FlowBot 自带），作为容错第二通道
