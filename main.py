import asyncio
import base64
import json
import os
import tempfile
import time
from collections import deque
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

try:
    from websockets.asyncio.client import connect as ws_connect  # websockets >= 13
    from websockets.exceptions import ConnectionClosed as WSConnectionClosed
except ImportError:  # pragma: no cover
    try:
        from websockets.client import connect as ws_connect  # websockets 10-12
        from websockets.exceptions import ConnectionClosed as WSConnectionClosed
    except ImportError:
        ws_connect = None
        WSConnectionClosed = Exception

if aiohttp is None or ws_connect is None:
    raise ImportError(
        "astrbot_plugin_flowbot_adapter 依赖缺失，请先安装: pip install aiohttp websockets"
    )

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.api.star import Context, Star
from astrbot.core.platform.register import register_platform_adapter

_MAX_RECONNECT = 60
_DEFAULT_RECONNECT = 5
_DEFAULT_MAX_RECONNECT_ATTEMPTS = 5
_DEDUP_WINDOW = 600
_RECENT_SEND_TTL = 3


@register_platform_adapter(
    "flowbot_adapter",
    "FlowBot 平台适配器（基于 FlowBot Docker WebUI 统一端口 7300，WS 入站 + HTTP 出站）",
    default_config_tmpl={
        "flowbot_host": "",
        "flowbot_port": 7400,
        "flowbot_api_key": "",
        "flowbot_reconnect_interval": _DEFAULT_RECONNECT,
        "flowbot_reconnect_max_attempts": _DEFAULT_MAX_RECONNECT_ATTEMPTS,
        "flowbot_use_direct_url": False,
        "flowbot_image_size_threshold": 10,
    },
    config_metadata={
        "flowbot_host": {
            "description": "FlowBot WebUI 主机地址",
            "type": "string",
            "hint": "请填写你的 FlowBot 的容器 IP",
        },
        "flowbot_port": {
            "description": "FlowBot 插件 API 端口",
            "type": "int",
            "hint": "FlowBot 插件 API 端口，默认 7400（注意：7300 是 WebUI 需登录，API Key 无效）",
        },
        "flowbot_api_key": {
            "description": "FlowBot WebUI API Key",
            "type": "string",
            "hint": "Docker 环境变量 weflow_webui_api_key 的值",
            "secret": True,
        },
        "flowbot_reconnect_interval": {
            "description": "断线重连初始间隔（秒）",
            "type": "int",
            "hint": f"默认 {_DEFAULT_RECONNECT}；指数退避，上限 {_MAX_RECONNECT}s",
        },
        "flowbot_reconnect_max_attempts": {
            "description": "断线重连最大次数",
            "type": "int",
            "hint": f"连续断线重连超过该次数即停止并退出，避免无限重试影响性能与日志。默认 {_DEFAULT_MAX_RECONNECT_ATTEMPTS}；填 0 表示不限制",
        },
        "flowbot_use_direct_url": {
            "description": "允许透传图片 URL",
            "type": "bool",
            "hint": "开启后，若图片源为 http(s) URL 则直接透传 image_url（省流量）；发送失败会自动回退 base64",
        },
        "flowbot_image_size_threshold": {
            "description": "图片 base64 阈值（MB）",
            "type": "int",
            "hint": "超过该大小的图片不再用 base64，改为透传 URL 或尝试上传获取 token。FlowBot body 上限 20MB（base64 约承载 15MB 原图），默认 10",
        },
    },
)
class FlowBotPlatform(Platform):
    """FlowBot 平台适配器：通过 FlowBot Docker WebUI 统一端口收发微信消息。

    入站：WebSocket /api/v1/ws/messages?token=<api_key>（归一化消息推送）
    出站：HTTP POST /api/v1/messages/send（Bearer API Key 认证）
    """

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict | None = None,
        event_queue: asyncio.Queue | None = None,
    ):
        if event_queue is None:
            if isinstance(platform_settings, asyncio.Queue):
                event_queue = platform_settings
                platform_settings = None
            else:
                event_queue = asyncio.Queue()
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings or {}

        host = str(self.config.get("flowbot_host", "") or "").strip() or "127.0.0.1"
        port = int(self.config.get("flowbot_port", 7400) or 7400)
        self._api_key = str(self.config.get("flowbot_api_key", "") or "").strip()
        self._webui_base = f"http://{host}:{port}"
        self._ws_url = f"ws://{host}:{port}/api/v1/ws/messages?token={self._api_key}"

        self._stop_event = asyncio.Event()
        self._ws_task: asyncio.Task | None = None
        self._loader_task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None

        self._seen_ids: dict[str, float] = {}
        self._recent_sends: deque[tuple[str, str, float]] = deque(maxlen=50)
        self._sessions_cache: dict[str, dict] = {}
        self._stats = {"recv": 0, "sent": 0}

    # ── 基础 ──────────────────────────────────────────────────────────

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="flowbot_adapter",
            description="FlowBot 平台适配器（FlowBot Docker WebUI 通道）",
            id=self.config.get("id", "flowbot_adapter"),
            adapter_display_name="FlowBot",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    async def run(self):
        self._stop_event.clear()
        if self._loader_task is None or self._loader_task.done():
            self._loader_task = asyncio.create_task(self._load_sessions_loop())
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        try:
            await self._ws_task
        except asyncio.CancelledError:
            pass

    async def terminate(self):
        self._stop_event.set()
        for task in (self._ws_task, self._loader_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._http is not None:
            await self._http.close()
            self._http = None
        logger.info("FlowBot adapter 已终止")

    def get_stats(self) -> dict:
        return dict(self._stats)

    # ── HTTP 客户端 ───────────────────────────────────────────────────

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        return self._http

    async def _api_json(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict | list | None:
        http = await self._ensure_http()
        url = f"{self._webui_base}{path}"
        try:
            if method == "GET":
                async with http.get(url) as resp:
                    if resp.status == 401:
                        logger.error(f"FlowBot API Key 认证失败（401）: {path}")
                        return None
                    text = await resp.text()
                    if resp.status >= 400:
                        logger.error(
                            f"FlowBot API 错误 {resp.status}: {path} -> {text[:200]}"
                        )
                        return None
                    return await _parse_json(text)
            async with http.post(url, json=payload or {}) as resp:
                text = await resp.text()
                if resp.status == 401:
                    logger.error(f"FlowBot API Key 认证失败（401）: {path}")
                    return None
                if resp.status >= 400:
                    logger.error(
                        f"FlowBot API 错误 {resp.status}: {path} -> {text[:200]}"
                    )
                    return None
                return await _parse_json(text)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"FlowBot HTTP 请求失败 {method} {path}: {e}")
            return None

    # ── 入站：WebSocket 消费 ──────────────────────────────────────────

    async def _run_ws_loop(self):
        delay = max(
            1,
            int(
                self.config.get("flowbot_reconnect_interval", _DEFAULT_RECONNECT)
                or _DEFAULT_RECONNECT
            ),
        )
        try:
            max_attempts = int(
                self.config.get("flowbot_reconnect_max_attempts", _DEFAULT_MAX_RECONNECT_ATTEMPTS)
                or _DEFAULT_MAX_RECONNECT_ATTEMPTS
            )
        except (TypeError, ValueError):
            max_attempts = _DEFAULT_MAX_RECONNECT_ATTEMPTS
        attempts = 0
        while not self._stop_event.is_set():
            try:
                async with ws_connect(self._ws_url, ping_interval=None) as ws:
                    logger.info(
                        f"FlowBot WS 已连接: ws://{_redact_host(self._webui_base)}/api/v1/ws/messages"
                    )
                    attempts = 0  # 连接成功，重置连续失败计数
                    delay = max(
                        1,
                        int(
                            self.config.get("flowbot_reconnect_interval", _DEFAULT_RECONNECT)
                            or _DEFAULT_RECONNECT
                        ),
                    )
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "ignore")
                        await self._on_frame(raw)
            except WSConnectionClosed as e:
                logger.warning(f"FlowBot WS 连接关闭: {e}")
            except Exception as e:
                logger.error(f"FlowBot WS 连接异常: {e}")
            if self._stop_event.is_set():
                break
            attempts += 1
            if max_attempts > 0 and attempts > max_attempts:
                logger.error(
                    f"FlowBot WS 连续断线重连超过 {max_attempts} 次，停止重连。"
                    f"请检查 FlowBot 服务（{_redact_host(self._webui_base)}）与配置"
                )
                break
            logger.info(f"FlowBot WS 第 {attempts} 次重连，{delay}s 后重试...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_RECONNECT)

    async def _on_frame(self, raw: str):
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"FlowBot WS 非 JSON 帧: {raw[:100]}")
            return
        event = frame.get("event")
        data = frame.get("data") or {}
        if event != "message":
            logger.debug(f"FlowBot WS 事件: {event}")
            return
        await self._handle_message(data)

    async def _handle_message(self, data: dict):
        message_id = str(data.get("message_id") or "")
        session_id = str(data.get("session_id") or "")
        if not message_id or not session_id:
            return
        now = time.time()
        if message_id in self._seen_ids:
            return
        self._seen_ids[message_id] = now
        if len(self._seen_ids) > 5000:
            expired = [k for k, t in self._seen_ids.items() if now - t > _DEDUP_WINDOW]
            for k in expired:
                self._seen_ids.pop(k, None)

        # 过滤自己发送的消息（发送回显 ping-pong）
        if self._is_recent_send(session_id, data.get("content", "")):
            return

        self._sessions_cache[session_id] = data
        try:
            abm = await self.convert_message(data)
        except Exception as e:
            logger.error(f"FlowBot 消息转换失败: {e}")
            return
        await self.handle_msg(abm)
        self._stats["recv"] += 1

    def _is_recent_send(self, session_id: str, content: str) -> bool:
        now = time.time()
        if not content:
            return False
        for sid, text, ts in self._recent_sends:
            if now - ts <= _RECENT_SEND_TTL and sid == session_id and text == content:
                return True
        return False

    def _mark_sent(self, session_id: str, content: str):
        self._recent_sends.append((session_id, content, time.time()))

    # ── 消息转换 ───────────────────────────────────────────────────────

    async def convert_message(self, data: dict) -> AstrBotMessage:
        abm = AstrBotMessage()
        session_type = str(data.get("session_type") or "private")
        is_group = session_type == "group" or "@chatroom" in str(
            data.get("session_id") or ""
        )
        abm.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
        abm.session_id = str(data.get("session_id") or "")
        abm.message_id = str(data.get("message_id") or "")
        abm.sender = MessageMember(
            user_id=str(data.get("sender_id") or ""),
            nickname=str(data.get("sender_name") or "")
            or str(data.get("sender_id") or ""),
        )
        abm.raw_message = data

        mtype = str(data.get("type") or "text")
        text = str(data.get("content") or "")
        abm.message_str = text
        components = []
        if mtype == "image":
            url = data.get("image_url") or (text if text.startswith("http") else "")
            local = (
                await self._download_image(self._fix_image_url(url)) if url else None
            )
            components.append(
                Image(file=local, url=url) if local else Plain(text=f"[图片] {text}")
            )
        elif mtype == "emoji":
            emoji_url = data.get("emoji_url") or ""
            local = (
                await self._download_image(self._fix_image_url(emoji_url))
                if emoji_url
                else None
            )
            components.append(
                Image(file=local, url=emoji_url) if local else Plain(text=text)
            )
        else:
            components.append(Plain(text=text))
        abm.message = components
        return abm

    async def handle_msg(self, message: AstrBotMessage):
        event = FlowBotMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            platform=self,
        )
        self.commit_event(event)

    def _fix_image_url(self, url: str) -> str:
        """将容器内视角的 127.0.0.1 直链改写为宿主机可达地址。"""
        try:
            u = urlparse(url)
            if u.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
                return f"{self._webui_base}{u.path}" + (
                    f"?{u.query}" if u.query else ""
                )
        except Exception:
            pass
        return url

    async def _download_image(self, url: str) -> str | None:
        """下载图片到临时文件，返回本地路径。超时 15s。"""
        if not url:
            return None
        http = await self._ensure_http()
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with http.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning(f"图片下载失败 {resp.status}: {url[:120]}")
                    return None
                data = await resp.read()
                suffix = _guess_suffix(url)
                fd, path = tempfile.mkstemp(suffix=suffix)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                return path
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"图片下载异常: {e}")
            return None

    # ── 出站：发送 ─────────────────────────────────────────────────────

    async def send_by_session(self, session, message_chain: MessageChain):
        raw = str(getattr(session, "session_id", "") or "")
        session_id = raw.split(":", 2)[-1]
        await self._send_to_session(session_id, message_chain)

    async def _send_to_session(self, session_id: str, message_chain: MessageChain):
        text_parts: list[str] = []
        images: list[Image] = []
        at_users: list[str] = []
        reply_to: str | None = None
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
            elif isinstance(comp, At):
                at_users.append(comp.uid)
            elif isinstance(comp, Reply):
                reply_to = str(comp.id or "")
            elif isinstance(comp, Image):
                images.append(comp)

        if text_parts or at_users:
            text = "".join(text_parts)
            await self._send_text(session_id, text, at_users, reply_to)
            self._mark_sent(session_id, text)
        for img in images:
            await self._send_image(session_id, img, reply_to)
            self._mark_sent(session_id, "")

    async def _send_text(
        self, session_id: str, content: str, at_users: list[str], reply_to: str | None
    ):
        payload: dict = {"session_id": session_id, "type": "text", "content": content}
        if at_users:
            payload["at_users"] = at_users
        if reply_to:
            payload["reply_to"] = reply_to
        result = await self._api_json("POST", "/api/v1/messages/send", payload)
        if result is not None:
            self._stats["sent"] += 1
            logger.info(f"FlowBot text -> {session_id}: {content[:50]}")

    async def _send_image(self, session_id: str, comp: Image, reply_to: str | None):
        raw = (getattr(comp, "file", None) or "").strip()
        url = (getattr(comp, "url", None) or "").strip()
        comp_path = (getattr(comp, "path", None) or "").strip()
        use_direct_url = bool(self.config.get("flowbot_use_direct_url", False))
        try:
            threshold_bytes = (
                max(1, int(self.config.get("flowbot_image_size_threshold", 10) or 10))
                * 1024
                * 1024
            )
        except (TypeError, ValueError):
            threshold_bytes = 10 * 1024 * 1024

        payload: dict = {"session_id": session_id, "type": "image"}
        if reply_to:
            payload["reply_to"] = reply_to

        # 候选源按 file/url/path 三字段依次尝试
        def _pick(candidates):
            for c in candidates:
                if c:
                    return c
            return ""

        b64_source = ""
        candidate = _pick((raw, url, comp_path))
        if candidate.startswith("base64://"):
            b64_source = candidate[len("base64://") :]
        elif candidate.startswith("data:") and "," in candidate:
            b64_source = candidate.split(",", 1)[-1]

        if b64_source:
            b64_source = b64_source.strip()
            if not b64_source:
                logger.warning(
                    f"FlowBot 图片发送失败: 空的 base64 源 (session={session_id})"
                )
                return
            # 估算原始大小 = base64 长度 × 3/4
            size = len(b64_source) * 3 // 4
            if size > threshold_bytes:
                token = await self._upload_media(b64=b64_source)
                if token:
                    payload["image_token"] = token
                else:
                    logger.warning(
                        f"FlowBot 图片 {size} 字节超过阈值 {threshold_bytes}，"
                        f"且上传失败，已跳过"
                    )
                    return
            else:
                payload["image_base64"] = b64_source
            result = await self._api_json("POST", "/api/v1/messages/send", payload)
            if result is not None:
                self._stats["sent"] += 1
                logger.info(f"FlowBot image(base64) -> {session_id}")
            return

        # ── file:/// 形态：剥前缀后按本地路径处理 ──
        def _strip_file_uri(val: str) -> str:
            if val.startswith("file:///"):
                return val[len("file:///") :].strip()
            return val

        raw = _strip_file_uri(raw)
        url = _strip_file_uri(url)
        comp_path = _strip_file_uri(comp_path)

        direct_url = ""
        for candidate in (url, raw):
            if candidate.startswith("http"):
                direct_url = self._fix_image_url(candidate)
                break

        local_path = ""
        for candidate in (raw, url, comp_path):
            if candidate and os.path.isfile(candidate):
                local_path = candidate
                break
        if not local_path and direct_url:
            # 无论是否透传 URL，都先下载一份本地缓存，供 image_url 发送失败时回退 base64
            local_path = await self._download_image(direct_url)
            if use_direct_url and not local_path:
                logger.debug("FlowBot 图片兜底下载失败，继续透传 URL")

        if use_direct_url and direct_url:
            payload["image_url"] = direct_url
        elif local_path:
            size = os.path.getsize(local_path)
            if size > threshold_bytes and direct_url:
                payload["image_url"] = direct_url
            elif size > threshold_bytes:
                token = await self._upload_media(local_path=local_path)
                if token:
                    payload["image_token"] = token
                else:
                    logger.warning(
                        f"FlowBot 图片 {size} 字节超过阈值 {threshold_bytes}，"
                        f"且无 URL 可透传、上传失败，已跳过"
                    )
                    return
            else:
                try:
                    with open(local_path, "rb") as f:
                        payload["image_base64"] = base64.b64encode(f.read()).decode(
                            "ascii"
                        )
                except Exception as e:
                    logger.warning(f"FlowBot 图片 base64 读取失败: {e}")
                    return
                payload["image_path"] = local_path  # 同主机部署兼容
        else:
            # 兜底：走 AstrBot 官方归一化（统一处理 base64:// file:/// http 纯路径）
            try:
                logger.debug(
                    f"FlowBot 图片字段 file={raw[:40]!r} url={url[:40]!r} "
                    f"path={comp_path[:40]!r}，尝试 convert_to_base64"
                )
                b64_source = await comp.convert_to_base64()
            except Exception as e:
                logger.warning(
                    f"FlowBot 图片发送失败: 无可用图片源 (session={session_id}): {e}"
                )
                return
            if b64_source:
                payload["image_base64"] = b64_source.strip()
                result = await self._api_json(
                    "POST", "/api/v1/messages/send", payload
                )
                if result is not None:
                    self._stats["sent"] += 1
                    logger.info(f"FlowBot image(convert) -> {session_id}")
                return

        result = await self._api_json("POST", "/api/v1/messages/send", payload)
        # image_url 透传失败（如 URL 不可达）时，若本地有文件则回退以 base64 重发
        if (
            result is None
            and "image_url" in payload
            and local_path
            and os.path.isfile(local_path)
        ):
            logger.warning(f"FlowBot image_url 发送失败，回退 image_base64: {session_id}")
            retry: dict = {"session_id": session_id, "type": "image"}
            if reply_to:
                retry["reply_to"] = reply_to
            try:
                with open(local_path, "rb") as f:
                    retry["image_base64"] = base64.b64encode(f.read()).decode("ascii")
            except Exception as e:
                logger.warning(f"FlowBot 图片 base64 回退读取失败: {e}")
                return
            retry["image_path"] = local_path
            result = await self._api_json("POST", "/api/v1/messages/send", retry)
        if result is not None:
            self._stats["sent"] += 1
            logger.info(f"FlowBot image -> {session_id}")

    async def _upload_media(
        self, local_path: str | None = None, b64: str | None = None
    ) -> str | None:
        """上传媒体到 FlowBot，返回 image_token；失败返回 None。

        FlowBot /api/v1/media/upload 只接受 JSON body（parseBody 仅 JSON.parse），
        不接收 multipart/form-data，故以 {"image_base64": ...} 提交。
        支持 local_path（读文件转 base64）或直接传 b64 字符串两种来源。
        """
        if not b64:
            if not local_path:
                return None
            try:
                with open(local_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
            except OSError as e:
                logger.warning(f"FlowBot 媒体读取失败: {e}")
                return None
        http = await self._ensure_http()
        url = f"{self._webui_base}/api/v1/media/upload"
        try:
            payload = {"image_base64": b64}
            async with http.post(url, json=payload) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning(
                        f"FlowBot 媒体上传失败 {resp.status}: {text[:200]}"
                    )
                    return None
                result = await _parse_json(text)
                if isinstance(result, dict):
                    inner = result.get("data")
                    for container in (inner, result):
                        if not isinstance(container, dict):
                            continue
                        for k in ("image_token", "token", "file_token", "media_id"):
                            if container.get(k):
                                return str(container[k])
                    if isinstance(inner, str) and inner:
                        return inner
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"FlowBot 媒体上传异常: {e}")
            return None

    # ── 会话预载（昵称映射） ─────────────────────────────────────────────

    async def _load_sessions_loop(self):
        while not self._stop_event.is_set():
            try:
                data = await self._api_json("GET", "/api/v1/sessions")
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("data") or data.get("sessions") or []
                for item in items:
                    if isinstance(item, dict):
                        sid = item.get("session_id") or item.get("id")
                        if sid:
                            self._sessions_cache[str(sid)] = item
                if items:
                    logger.info(f"FlowBot 会话预载完成: {len(items)} 个会话")
            except Exception as e:
                logger.debug(f"FlowBot 会话预载失败: {e}")
            await asyncio.sleep(300)


class FlowBotMessageEvent(AstrMessageEvent):
    """FlowBot 消息事件：send() 直接走平台原生发送通道。"""

    def __init__(
        self,
        message_str,
        message_obj,
        platform_meta,
        session_id,
        platform: FlowBotPlatform,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._platform = platform

    async def send(self, message: MessageChain):
        await self._platform._send_to_session(self.get_sender_id(), message)
        await super().send(message)


class FlowBotAdapterPlugin(Star):
    """FlowBot 适配器插件入口。

    平台适配器通过 @register_platform_adapter 装饰器注册（模块导入时生效），
    插件本体仍需提供 Star 子类作为加载入口，否则 AstrBot 无法识别插件已注册。
    """

    def __init__(self, context: Context):
        super().__init__(context)


# ── 工具函数 ─────────────────────────────────────────────────────────


async def _parse_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _guess_suffix(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        if path.endswith(ext):
            return ext
    return ".jpg"


def _redact_host(base: str) -> str:
    """隐藏日志中的 host（不泄露内网地址细节）。"""
    return base.replace("http://", "").replace("https://", "")
