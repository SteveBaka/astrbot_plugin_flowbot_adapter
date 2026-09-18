import asyncio
import base64
import json
import os
import re
import tempfile
import time
from collections import deque
from urllib.parse import quote, urlparse

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
from astrbot.api.message_components import (
    At,
    AtAll,
    BaseMessageComponent,
    ComponentType,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.api.star import Context, Star
from astrbot.core.platform.register import register_platform_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

_MAX_RECONNECT = 60
_DEFAULT_RECONNECT = 5
_DEFAULT_MAX_RECONNECT_ATTEMPTS = 5
_DEDUP_WINDOW = 600
_RECENT_SEND_TTL = 3
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 单次图片下载大小上限 5MB（flowbot 硬上限）
_WS_PING_INTERVAL = 30  # WebSocket 心跳间隔（秒），用于检测半开连接
_MAX_TEMP_FILES = 100  # 本地临时文件追踪上限，超出清理最旧，防止无限膨胀

# ── 入站媒体下载常量（视频/语音，FlowBot videoMaxBytes/voiceMaxBytes 对齐） ──
_MAX_VIDEO_DOWNLOAD_BYTES = 100 * 1024 * 1024
_VIDEO_DOWNLOAD_TIMEOUT = 120
_MAX_VIDEO_TEMP_FILES = 10  # 视频临时文件独立上限（单文件可达百 MB，从严）
_VIDEO_TEMP_TTL = 1800
_MAX_VOICE_DOWNLOAD_BYTES = 10 * 1024 * 1024
_VOICE_DOWNLOAD_TIMEOUT = 60
_BARE_B64_MIN_LEN = 64  # 裸 base64 识别的最小长度阈值，避免误伤短路径/URL
_B64_CHARSET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_SESSIONS_TTL = 60  # 群列表缓存 TTL（秒）
_MEMBERS_TTL = 60  # 群成员缓存 TTL（秒）


@register_platform_adapter(
    "flowbot_adapter",
    "FlowBot 平台适配器（基于 FlowBot Docker WebUI 统一端口 7300，WS 入站 + HTTP 出站）",
    logo_path="logo.png",
    default_config_tmpl={
        "flowbot_host": "",
        "flowbot_port": 7400,
        "flowbot_api_key": "",
        "flowbot_reconnect_interval": _DEFAULT_RECONNECT,
        "flowbot_reconnect_max_attempts": _DEFAULT_MAX_RECONNECT_ATTEMPTS,
        "flowbot_use_direct_url": False,
        "flowbot_image_size_threshold": 5,
        "flowbot_video_size_threshold": 14,
        "flowbot_video_use_direct_url": True,
        "flowbot_text_split_enabled": True,
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
            "description": "FlowBot Bot Token",
            "type": "string",
            "hint": "FlowBot WebUI 中插件模式 Bot 配置的 Token（创建 Bot 时自动生成），与 WebUI 登录密码无关",
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
            "hint": "超过该大小的图片不再用 base64 直发（避免微信粘贴大图冻结），改为透传 URL 或尝试上传获取 token。默认 5；下载/上传硬上限 10MB",
        },
        "flowbot_video_size_threshold": {
            "description": "视频文件 base64 阈值（MB）",
            "type": "int",
            "hint": "≤该大小的视频以 video_base64 直传；超过则以 video_url 直链由 FlowBot 下载。默认 14（对齐 FlowBot 20MB body ÷ 1.33）；上限约 100MB",
        },
        "flowbot_video_use_direct_url": {
            "description": "视频直链优先",
            "type": "bool",
            "hint": "与图片相反：视频默认走直链（大、更稳）。开启后优先透传 video_url，失败回退 base64",
        },
        "flowbot_text_split_enabled": {
            "description": "空行分段发送",
            "type": "bool",
            "hint": "把正文中的空行（两个及以上换行）视为多条消息分隔符，拆成多次微信消息依次发送（@ 与引用仅挂在第一条）。兼容 outputpro 等分段插件不支持的平台的兜底；默认开",
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
        self._http: aiohttp.ClientSession | None = None
        # 机器人真实 wxid：启动时经 /api/v1/bot/self 预热 + 推送学习，落盘跨重启
        self._bot_wxid = self._load_bot_wxid_cache()

        self._seen_ids: dict[str, float] = {}
        self._recent_sends: deque[tuple[str, str, float]] = deque(maxlen=50)
        self._sessions_cache: dict[str, dict] = {}
        self._MAX_SESSIONS_CACHE = 2000
        self._temp_files: dict[str, float] = {}  # path -> mtime，用于容量清理
        self._video_temp_files: dict[str, float] = {}  # 入站视频临时文件（独立短 TTL）
        self._stats = {"recv": 0, "sent": 0}
        # 按需懒加载缓存：key -> (fetched_at, value)
        self._group_cache: dict[str, tuple[float, list]] = {}
        # 成员缓存：group_id -> (fetched_at, list[MessageMember], list[原始 member dict])
        # 保留原始 dict 以便 get_member_avatar_url 直接复用同一份请求结果（头像 URL 在 avatarUrl）。
        self._member_cache: dict[str, tuple[float, list, list]] = {}
        # 入站推送的群头像缓存：group_id -> group_avatar_url（flowbot 新契约 group_avatar_url）
        self._inbound_group_avatars: dict[str, str] = {}

    def _cache_session(self, session_id: str, item: dict):
        """写入会话缓存，超出容量时清理最旧条目，防止无限增长。"""
        if not session_id:
            return
        self._sessions_cache[session_id] = item
        if len(self._sessions_cache) > self._MAX_SESSIONS_CACHE:
            for k in list(self._sessions_cache)[
                : len(self._sessions_cache) - self._MAX_SESSIONS_CACHE
            ]:
                self._sessions_cache.pop(k, None)

    # ── 群/成员按需查询（懒加载 + TTL 缓存，供第三方插件使用） ───────────

    async def get_group_list(self) -> list[Group]:
        """查询群列表（GET /api/v1/sessions，sessionType==group）。按需懒加载，60s 缓存。"""
        now = time.time()
        cached = self._group_cache.get("__all__")
        if cached and now - cached[0] < _SESSIONS_TTL:
            return cached[1]
        data = await self._api_json("GET", "/api/v1/sessions")
        items = []
        if isinstance(data, dict):
            items = data.get("sessions") or []
        elif isinstance(data, list):
            items = data
        groups = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("sessionType") or "").lower() != "group":
                continue
            groups.append(
                Group(
                    group_id=str(item.get("username") or ""),
                    group_name=str(item.get("displayName") or ""),
                    group_avatar=str(item.get("avatarUrl") or "") or None,
                )
            )
        self._group_cache["__all__"] = (now, groups)
        return groups

    async def get_group_info(self, group_id: str) -> Group | None:
        """查询单个群信息（从群列表缓存中匹配）。"""
        groups = await self.get_group_list()
        for g in groups:
            if g.group_id == group_id:
                return g
        return None

    async def get_group_avatar(self, group_id: str) -> str | None:
        """查询群头像 URL：优先入站推送的 group_avatar_url（实时），其次 sessions enrich 的 avatarUrl。"""
        if group_id and group_id in self._inbound_group_avatars:
            return self._inbound_group_avatars.get(group_id) or None
        info = await self.get_group_info(group_id)
        return getattr(info, "group_avatar", None) or None

    async def get_member_list(
        self, group_id: str, force_refresh: bool = False
    ) -> list[MessageMember]:
        """查询群成员列表（GET /api/v1/group-members）。按需懒加载，60s 缓存。

        force_refresh=True 时强制绕过 TTL 刷新（供调用方在成员缺失昵称/头像时自救）。
        上游请求失败时不写空缓存，回退返回过期的已缓存成员，避免短暂接口故障造成
        整组成员"无昵称/无头像"长达 TTL。
        """
        now = time.time()
        cached = self._member_cache.get(group_id)
        if not force_refresh and cached and now - cached[0] < _MEMBERS_TTL:
            return cached[1]
        url = f"/api/v1/group-members?chatroomId={quote(group_id)}&forceRefresh={1 if force_refresh else 0}"
        data = await self._api_json("GET", url)
        if not isinstance(data, dict):
            # 上游失败：不缓存空结果，尽量复用过期缓存，避免 60s 空缓存污染。
            if cached:
                return cached[1]
            return []
        items = data.get("members") or []
        members = []
        raw_items: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_items.append(item)
            members.append(
                MessageMember(
                    user_id=str(item.get("wxid") or ""),
                    nickname=str(
                        item.get("displayName")
                        or item.get("groupNickname")
                        or item.get("nickname")
                        or ""
                    ),
                )
            )
        self._member_cache[group_id] = (now, members, raw_items)
        return members

    async def get_member_avatar_url(
        self, group_id: str, user_id: str, force_refresh: bool = False
    ) -> str | None:
        """查询群成员头像 URL（GET /api/v1/group-members 的 avatarUrl 字段）。

        复用 get_member_list 缓存的原始 member dict，命中 TTL 内不再重复请求接口。
        force_refresh=True 时强制刷新（某成员头像在群内但缓存缺失时排查用）。
        """
        now = time.time()
        cached = self._member_cache.get(group_id)
        items: list[dict] | None = None
        if not force_refresh and cached and now - cached[0] < _MEMBERS_TTL:
            items = cached[2]
        if items is None:
            await self.get_member_list(group_id, force_refresh=force_refresh)
            refreshed = self._member_cache.get(group_id)
            items = refreshed[2] if refreshed else None
        if not items:
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            if _wxid_match(str(item.get("wxid") or ""), user_id):
                return str(item.get("avatarUrl") or "") or None
        return None

    async def get_member_info(self, group_id: str, user_id: str) -> MessageMember | None:
        """查询单个群成员信息（从成员列表缓存中匹配）。"""
        members = await self.get_member_list(group_id)
        for m in members:
            if _wxid_match(m.user_id, user_id):
                return m
        return None

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

    @property
    def platform(self) -> str:
        """平台标识：供第三方插件按标准接口探测（如 _detect_platform_name）。"""
        return "flowbot_adapter"

    def get_client(self) -> object:
        """返回客户端对象自身，供第三方插件按 AstrBot Platform 标准发现本适配器。"""
        return self

    async def run(self):
        self._stop_event.clear()
        # 预热先于 WS：保证 wxid 缓存在首条事件到达前就绪
        try:
            await self._fetch_bot_wxid()
        except Exception as e:
            logger.info(f"FlowBot wxid 预热不可用，回退推送学习: {e}")
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        try:
            await self._ws_task
        except asyncio.CancelledError:
            pass

    async def terminate(self):
        self._stop_event.set()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._cleanup_temp_files()
        logger.info("FlowBot adapter 已终止")

    def get_stats(self) -> dict:
        """基类契约字段（id/status/started_at/error_count/meta 等）必须保留：
        Dashboard 依此判定平台运行状态，整体覆盖会导致状态显示为「未知」。"""
        stats = super().get_stats()
        stats.update(self._stats)
        return stats

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

    def _config_int(self, key: str, default: int) -> int:
        """安全读取 int 配置，非法值回退默认。"""
        try:
            return int(self.config.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    async def _run_ws_loop(self):
        delay = max(
            1, self._config_int("flowbot_reconnect_interval", _DEFAULT_RECONNECT)
        )
        max_attempts = self._config_int(
            "flowbot_reconnect_max_attempts", _DEFAULT_MAX_RECONNECT_ATTEMPTS
        )
        attempts = 0
        while not self._stop_event.is_set():
            try:
                async with ws_connect(
                    self._ws_url, ping_interval=_WS_PING_INTERVAL
                ) as ws:
                    logger.info(
                        f"FlowBot WS 已连接: ws://{_mask_host(self._webui_base)}/api/v1/ws/messages"
                    )
                    attempts = 0  # 连接成功，重置连续失败计数
                    delay = max(
                        1,
                        self._config_int(
                            "flowbot_reconnect_interval", _DEFAULT_RECONNECT
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
                    f"请检查 FlowBot 服务（{_mask_host(self._webui_base)}）与配置"
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

        # 过滤自己发送的消息（发送回显 ping-pong）：优先确定性判定
        # sender_id == self_id；仅当身份不可知（旧版服务端无 self_id）时
        # 才回退内容匹配，避免真人 3 秒内复读机器人被误丢
        self._remember_bot_wxid(str(data.get("self_id") or ""))
        sender_id = str(data.get("sender_id") or "")
        if self._bot_wxid:
            if sender_id == self._bot_wxid:
                return
        elif self._is_recent_send(session_id, data.get("content", "")):
            return

        self._cache_session(session_id, data)
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

    # ── 机器人身份（真实 wxid）：预热 / 学习 / 持久化 ──────────────────

    def _bot_wxid_cache_path(self) -> str:
        try:
            return os.path.join(get_astrbot_data_path(), "flowbot_adapter_bot_wxid")
        except Exception:
            return ""

    def _load_bot_wxid_cache(self) -> str:
        path = self._bot_wxid_cache_path()
        if not path:
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _remember_bot_wxid(self, wxid: str):
        wxid = str(wxid or "").strip()
        if not wxid or wxid == self._bot_wxid:
            return
        self._bot_wxid = wxid
        path = self._bot_wxid_cache_path()
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(wxid)
            except OSError:
                pass
        logger.info(f"FlowBot 机器人 wxid 已缓存: {wxid}")

    async def _fetch_bot_wxid(self):
        """启动预热：GET /api/v1/bot/self 取登录 wxid（Bot Token 鉴权）。
        source=config 为权威值，learned 为服务端身份库学习值兜底；
        失败（旧版服务端无此路由/网络未就绪）属预期回退，只记 INFO。"""
        try:
            http = await self._ensure_http()
            async with http.get(f"{self._webui_base}/api/v1/bot/self") as resp:
                if resp.status != 200:
                    logger.info(
                        f"FlowBot wxid 预热不可用（HTTP {resp.status}），回退推送学习"
                    )
                    return
                data = await _parse_json(await resp.text())
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.info(f"FlowBot wxid 预热不可用，回退推送学习: {e}")
            return
        if not isinstance(data, dict) or not data.get("ok"):
            logger.info("FlowBot wxid 预热不可用（响应异常），回退推送学习")
            return
        if str(data.get("source") or "") not in ("config", "learned"):
            return
        wxid = str(data.get("self_id") or "").strip()
        if wxid:
            self._remember_bot_wxid(wxid)

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
            nickname=str(
                data.get("sender_name")
                or data.get("sender_card")
                or data.get("source_name")
                or data.get("sender_id")
                or ""
            ),
        )
        # 机器人自身 wxid（flowbot 推送新增 self_id，供 AstrBot 识别"@登录账号"）
        abm.self_id = str(data.get("self_id") or "")
        # 消息时间戳：flowbot 推送含 unix 秒 timestamp（来源 WCDB createTime），
        # 透传真实发送时间，供时段统计/增量游标使用；缺省回退到达时间
        try:
            abm.timestamp = int(data.get("timestamp") or 0) or int(time.time())
        except (TypeError, ValueError):
            abm.timestamp = int(time.time())
        if is_group:
            # 为群消息设置群信息，避免 AstrBot 群上下文错误（回退到 sender.group_id）
            group_avatar = str(data.get("group_avatar_url") or "") or None
            if group_avatar:
                # flowbot 新契约：群消息置空 avatar_url、新增 group_avatar_url 承载群头像
                self._inbound_group_avatars[abm.session_id] = group_avatar
            abm.group = Group(
                group_id=abm.session_id,
                group_name=str(data.get("group_name") or ""),
                group_avatar=group_avatar,
            )
        abm.raw_message = data

        mtype = str(data.get("type") or "text")
        text = str(data.get("content") or "")
        abm.message_str = text

        components = []
        if mtype == "image":
            components.append(await self._normalize_inbound_image(data, text))
        elif mtype == "video":
            components.extend(await self._normalize_inbound_video(data, text))
        elif mtype == "voice":
            components.extend(await self._normalize_inbound_voice(data, text))
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

        # @ 唤醒：群里 @ 到机器人（flowbot 从消息 XML atuserlist 提取 at_users），
        # 生成 At 组件 → AstrBot 唤醒条件 At.qq == get_self_id() 命中
        at_users = data.get("at_users") or []
        if is_group and abm.self_id:
            at_targets = [str(x) for x in at_users]
            is_at_all = any(str(x).lower() in ("notify@all", "all") for x in at_targets)
            is_self_at = abm.self_id in at_targets
            if is_at_all:
                components.insert(0, AtAll())
            elif is_self_at:
                components.insert(
                    0,
                    At(qq=abm.self_id, name=str(data.get("group_name") or "")),
                )

        # 引用回复：is_self 时 sender_id 钉死为 self_id（触发无 @ 唤醒），他人原样透传
        quoted_sender_id = str(data.get("quoted_sender_id") or "")
        quoted_svrid = str(data.get("quoted_svrid") or "")
        if quoted_sender_id or quoted_svrid:
            quoted_is_self = bool(abm.self_id) and bool(data.get("quoted_is_self"))
            reply_sender_id = abm.self_id if quoted_is_self else quoted_sender_id
            reply_sender_name = str(data.get("quoted_sender_name") or "")
            reply_text = str(data.get("quoted_content") or "")
            try:
                # qq 字段为 int 校验（v4.27.4），wxid 字符串不可传
                components.insert(
                    0,
                    Reply(
                        id=quoted_svrid,
                        chain=[Plain(text=reply_text)] if reply_text else [],
                        sender_id=reply_sender_id,
                        sender_nickname=reply_sender_name,
                        message_str=reply_text,
                        text=reply_text,  # deprecated 兼容字段
                    ),
                )
            except Exception as e:
                # 版本差异防御：Reply 构造失败只降级为无引用文本，不丢消息
                logger.warning(f"FlowBot Reply 组件构造失败，降级为无引用文本: {e}")
        abm.message = components
        return abm

    async def _normalize_inbound_image(self, data: dict, text: str):
        """入站图片源归一化：base64://、data:、裸 base64、http(s)、本地路径。"""
        url = str(data.get("image_url") or "")
        base64_src = str(data.get("image_base64") or "")
        content = str(data.get("content") or "")

        # 1. 显式 base64:// / data: / 裸 base64 → 不落盘
        if base64_src:
            stripped = base64_src.strip()
            if stripped.startswith("base64://"):
                stripped = stripped[len("base64://") :]
            elif stripped.startswith("data:") and "," in stripped:
                stripped = stripped.split(",", 1)[-1]
            if stripped:
                return Image(file=f"base64://{stripped}", url="")

        # 2. 文本内嵌 base64（如 [图片]base64://... / data:image/png;base64,...）
        if content.startswith("base64://"):
            return Image(file=content, url="")
        if content.startswith("data:") and "," in content:
            b64_body = content.split(",", 1)[-1]
            if _looks_like_base64(b64_body):
                return Image(file=f"base64://{b64_body}", url="")

        # 3. 裸 base64 文本
        if _looks_like_base64(content):
            return Image(file=f"base64://{content}", url="")

        # 4. http(s) URL → 下载（已含 _fix_image_url 容器内地址改写）
        if url.startswith("http"):
            local = await self._download_image(self._fix_image_url(url))
            if local:
                return Image(file=local, url=url)

        # 5. content 本身是 http URL（无 image_url 字段时的兜底）
        if content.startswith("http"):
            local = await self._download_image(self._fix_image_url(content))
            if local:
                return Image(file=local, url=content)

        # 6. 兜底：文本占位
        return Plain(text=f"[图片] {text}")

    async def _normalize_inbound_video(self, data: dict, text: str) -> list:
        """入站视频三档降级：视频本体 / 封面降级段（fileMissing）/ [视频] 占位。

        契约与字段语义见 FlowBot ADAPTER-MEDIA-CONTRACT §5.2/§5.3。"""
        video_url = str(data.get("video_url") or "")
        poster_url = str(data.get("video_poster_url") or "")
        meta = data.get("video_meta") or {}
        file_missing = bool(meta.get("fileMissing"))

        # 1. 视频本体：立即下载（token 1h 可重复，但尽早消费避免池淘汰/过期）
        if video_url and not file_missing:
            local = await self._download_video(video_url)
            if local:
                comp = Video(file=local)
                if poster_url:
                    poster = await self._download_image(poster_url)
                    if poster:
                        try:
                            comp.cover = poster
                        except Exception:
                            pass
                return [comp]
            logger.warning(
                f"FlowBot 入站视频下载失败，降级封面/占位: {video_url[:120]}"
            )

        # 2. 封面降级段（对齐 OneBot 通道行为矩阵：视频缺失但有封面）
        if poster_url:
            poster = await self._download_image(poster_url)
            if poster:
                return [
                    Image(file=poster),
                    Plain(text="（以上为视频封面截图，视频文件未下载）"),
                ]

        # 3. 兜底：与开关关闭时逐字一致的文本占位
        return [Plain(text=text or "[视频]")]

    async def _download_video(self, url: str) -> str | None:
        """流式下载入站视频（上限 100MB、120s 超时），失败返回 None。独立短 TTL 池。"""
        if not url:
            return None
        self._trim_video_temp_files()
        fd, path = tempfile.mkstemp(suffix=".mp4")
        try:
            timeout = aiohttp.ClientTimeout(total=_VIDEO_DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"视频下载失败 {resp.status}: {url[:120]}"
                        )
                        os.close(fd)
                        os.remove(path)
                        return None
                    written = 0
                    with os.fdopen(fd, "wb") as f:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            written += len(chunk)
                            if written > _MAX_VIDEO_DOWNLOAD_BYTES:
                                logger.warning(
                                    f"视频下载超过大小上限 "
                                    f"{_MAX_VIDEO_DOWNLOAD_BYTES}: {url[:120]}"
                                )
                                f.close()
                                os.remove(path)
                                return None
                            f.write(chunk)
            self._video_temp_files[path] = time.time()
            return path
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"视频下载异常: {e}")
            try:
                os.close(fd)
            except OSError:
                pass
            self._remove_quiet(path)
            return None
        except Exception as e:
            logger.warning(f"视频下载未知异常: {e}")
            try:
                os.close(fd)
            except OSError:
                pass
            self._remove_quiet(path)
            return None

    def _trim_video_temp_files(self):
        now = time.time()
        for p, ts in list(self._video_temp_files.items()):
            if now - ts > _VIDEO_TEMP_TTL:
                self._remove_quiet(p)
                self._video_temp_files.pop(p, None)
        if len(self._video_temp_files) <= _MAX_VIDEO_TEMP_FILES:
            return
        excess = len(self._video_temp_files) - _MAX_VIDEO_TEMP_FILES
        oldest = sorted(self._video_temp_files.items(), key=lambda kv: kv[1])[:excess]
        for p, _ in oldest:
            self._remove_quiet(p)
            self._video_temp_files.pop(p, None)

    def _remove_quiet(self, path: str):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    async def _normalize_inbound_voice(self, data: dict, text: str) -> list:
        """入站语音两档：WAV 下载 → Record 组件 / 带时长的 [语音] 占位。

        契约与字段语义见 FlowBot ADAPTER-MEDIA-CONTRACT §5.4。"""
        voice_url = str(data.get("voice_url") or "")
        duration_sec = data.get("voice_duration_sec")
        meta = data.get("voice_meta") or {}
        available = bool(meta.get("available", True))

        # 1. WAV 可得：HEAD 预检体积（/api/media 全链路支持，超限免流式中止）再下载
        if voice_url and available:
            local = await self._download_voice(voice_url)
            if local:
                try:
                    return [Record(file=local)]
                except Exception as e:
                    # 版本差异防御：Record 构造失败降级为文本占位，不丢消息
                    logger.warning(f"FlowBot Record 组件构造失败，降级为文本占位: {e}")
                    self._track_temp_file(local)
                    return [
                        Plain(
                            text=f"[语音({duration_sec}s)]"
                            if isinstance(duration_sec, (int, float))
                            else "[语音]"
                        )
                    ]
            logger.warning(f"FlowBot 入站语音下载失败，降级文本占位: {voice_url[:120]}")

        # 2. 兜底：与开关关闭时同构的文本占位（契约 §5.4：文本 + 时长降级）
        return [
            Plain(
                text=f"[语音({duration_sec}s)]"
                if isinstance(duration_sec, (int, float))
                else "[语音]"
            )
        ]

    async def _download_voice(self, url: str) -> str | None:
        """下载入站语音 WAV（HEAD 预检体积，上限 10MB、60s 超时），失败返回 None。"""
        if not url:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=_VOICE_DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.head(url) as head:
                    if head.status != 200:
                        logger.warning(f"语音预检失败 {head.status}: {url[:120]}")
                        return None
                    size = int(head.headers.get("Content-Length") or 0)
                    if size > _MAX_VOICE_DOWNLOAD_BYTES:
                        logger.warning(
                            f"语音超过大小上限 {_MAX_VOICE_DOWNLOAD_BYTES}: "
                            f"{size}B {url[:120]}"
                        )
                        return None
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"语音下载失败 {resp.status}: {url[:120]}")
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > _MAX_VOICE_DOWNLOAD_BYTES:
                            logger.warning(
                                f"语音下载超过大小上限 {_MAX_VOICE_DOWNLOAD_BYTES}: "
                                f"{url[:120]}"
                            )
                            return None
                        chunks.append(chunk)
            fd, path = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
            self._track_temp_file(path)
            return path
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"语音下载异常: {e}")
            return None

    async def handle_msg(self, message: AstrBotMessage):
        event = FlowBotMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            platform=self,
        )
        # 入站：将本适配器下载的临时图片/语音登记到事件周期，事件结束后由 AstrBot 清理
        for comp in message.message or []:
            if isinstance(comp, (Image, Record)):
                file_ref = getattr(comp, "file", None)
                if file_ref and file_ref in self._temp_files:
                    try:
                        event.track_temporary_local_file(file_ref)
                        self._forget_temp_file(file_ref)
                    except Exception as e:
                        logger.debug(f"FlowBot 临时媒体登记失败: {e}")
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
        """下载图片到临时文件，返回本地路径。超时 15s，大小上限 20MB。

        使用独立的无鉴权 session，避免把 FlowBot API Key 随下载请求泄露给第三方 URL。
        """
        if not url:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"图片下载失败 {resp.status}: {url[:120]}")
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            logger.warning(
                                f"图片下载超过大小上限 {_MAX_DOWNLOAD_BYTES}: {url[:120]}"
                            )
                            return None
                        chunks.append(chunk)
                    suffix = _guess_suffix(url)
                    fd, path = tempfile.mkstemp(suffix=suffix)
                    with os.fdopen(fd, "wb") as f:
                        for chunk in chunks:
                            f.write(chunk)
                    self._track_temp_file(path)
                    return path
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"图片下载异常: {e}")
            return None

    def _track_temp_file(self, path: str):
        """登记本适配器创建的临时文件，超出容量时清理最旧，防无限膨胀。"""
        if not path:
            return
        self._temp_files[path] = time.time()
        self._trim_temp_files()

    def _forget_temp_file(self, path: str):
        """解除临时文件登记（配合 track_temporary_local_file 由 AstrBot 清理）。"""
        if path:
            self._temp_files.pop(path, None)

    def _trim_temp_files(self):
        """超出容量上限时，删除最旧的临时文件。"""
        if len(self._temp_files) <= _MAX_TEMP_FILES:
            return
        excess = len(self._temp_files) - _MAX_TEMP_FILES
        oldest = sorted(self._temp_files.items(), key=lambda kv: kv[1])[:excess]
        for path, _ in oldest:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            self._temp_files.pop(path, None)

    def _cleanup_temp_files(self):
        """删除本适配器创建且仍未被使用/登记的临时文件。"""
        for path in list(self._temp_files):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        self._temp_files.clear()
        for path in list(self._video_temp_files):
            self._remove_quiet(path)
        self._video_temp_files.clear()

    # ── 出站：发送 ─────────────────────────────────────────────────────

    async def send_by_session(self, session, message_chain: MessageChain):
        raw = str(getattr(session, "session_id", "") or "")
        session_id = raw.split(":", 2)[-1]
        await self._send_to_session(session_id, message_chain)

    async def send_proactive_by_session(
        self, session, message_chain: MessageChain
    ) -> dict | None:
        """主动发送消息（AstrBot Platform 标准接口）。"""
        raw = str(getattr(session, "session_id", "") or "")
        session_id = raw.split(":", 2)[-1]
        await self._send_to_session(session_id, message_chain)
        return {"success": True}

    # ── 第三方插件可调用的便捷 API（经 context.get_platform_inst(id) 获取实例） ──

    def capabilities(self) -> dict:
        """能力声明：供第三方插件判断本适配器支持范围。"""
        return {
            "platform": "flowbot_adapter",
            "supports_text": True,
            "supports_image": True,
            "supports_file": True,  # flowbot Linux 容器经 type=video 通道发送文件/视频
            "supports_voice": False,
            "supports_video": True,
            "supports_at": True,
            "supports_reply": True,
            "supports_group": True,
            "supports_proactive": True,
            "supports_avatar": True,  # 群/成员头像（sessions/group-members enrich）
            "max_text_length": 1800,
            "image_max_bytes": 5 * 1024 * 1024,
        }

    async def send_text(
        self,
        session_id: str,
        content: str,
        at_users: list[str] | None = None,
        reply_to: str | None = None,
    ) -> bool:
        """便捷文本发送（第三方插件调用）。返回是否发送成功。"""
        payload: dict = {
            "session_id": session_id,
            "type": "text",
            "content": content or "",
        }
        if at_users:
            payload["at_users"] = at_users
        if reply_to:
            payload["reply_to"] = reply_to
        result = await self._api_json("POST", "/api/v1/messages/send", payload)
        if result is not None:
            self._stats["sent"] += 1
            self._mark_sent(session_id, content or "")
            return True
        return False

    async def send_image(
        self, session_id: str, image: Image | str
    ) -> bool:
        """便捷图片发送（第三方插件调用）。image 可为 Image 组件或路径/URL/base64 字符串。"""
        if isinstance(image, str):
            image = Image(file=image)
        await self._send_image(session_id, image, None)
        return True

    async def send_video(
        self, session_id: str, video: Video | str
    ) -> bool:
        """便捷视频发送（第三方插件调用）。video 可为 Video 组件或路径/URL/base64 字符串。"""
        if isinstance(video, str):
            video = Video(file=video)
        await self._send_video(session_id, video)
        return True

    async def send_message_chain(
        self, session_id: str, message_chain: MessageChain
    ) -> bool:
        """便捷消息链发送（第三方插件调用）。"""
        await self._send_to_session(session_id, message_chain)
        return True

    async def get_self_id(self) -> str:
        """机器人身份：真实登录 wxid（启动预热/推送学习），未学到时回退 meta id。"""
        return self._bot_wxid or str(self.config.get("id", "flowbot_adapter"))

    def get_bot_wxid(self) -> str:
        """真实登录 wxid（能力钩子，供第三方插件在非事件上下文查询；空=尚未学到）。"""
        return self._bot_wxid

    async def _send_to_session(self, session_id: str, message_chain: MessageChain):
        text_parts: list[str] = []
        images: list[Image] = []
        at_users: list[str] = []
        reply_to: str | None = None
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
            elif isinstance(comp, FlowBotMention):
                # 自研 wxid 组件：直接取 wxid（"all" = @全体）
                target = str(getattr(comp, "wxid", None) or "")
                if target == "all":
                    at_users.append("all")
                elif target:
                    at_users.append(target)
            elif isinstance(comp, At):
                # 兼容 AstrBot 内置 At（含第三方插件）：wxid → qq → uid
                target = str(
                    getattr(comp, "wxid", None)
                    or getattr(comp, "qq", None)
                    or getattr(comp, "uid", None)
                    or ""
                )
                if target == "all":
                    at_users.append("all")
                elif target:
                    at_users.append(target)
            elif isinstance(comp, Reply):
                reply_to = str(comp.id or "")
            elif isinstance(comp, Image):
                images.append(comp)
            elif isinstance(comp, Video):
                await self._send_video(session_id, comp)
                self._mark_sent(session_id, "")
            elif isinstance(comp, File):
                # flowbot Linux 容器经 type=video 通道发送文件/视频（微信端同为粘贴）
                await self._send_video(session_id, comp)
                self._mark_sent(session_id, "")

        # 空行合并 + 分段：多段文本以换行保留段落结构；\n\n 视为多条消息
        # 分隔符（上游分段插件对非白名单平台不分段时由适配器兜底拆发）
        text = "\n".join(text_parts).strip()
        if text:
            segments = [text]
            if bool(self.config.get("flowbot_text_split_enabled", True)):
                segments = [s.strip() for s in re.split(r"\n{2,}", text) if s.strip()]
            for i, seg in enumerate(segments):
                seg_at = at_users if i == 0 else []
                seg_reply = reply_to if i == 0 else None
                await self._send_text(session_id, seg, seg_at, seg_reply)
                self._mark_sent(session_id, seg)
        elif at_users and not images:
            # 纯 @ 无正文：无内容可发，跳过（避免 flowbot 400 Missing content）
            logger.debug(f"FlowBot 跳过空正文 @ 消息 (session={session_id})")
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
                max(1, int(self.config.get("flowbot_image_size_threshold", 5) or 5))
                * 1024
                * 1024
            )
        except (TypeError, ValueError):
            threshold_bytes = 5 * 1024 * 1024

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
        elif _looks_like_base64(candidate):
            # 裸 base64（无前缀）：仅当不可能是路径/URL 时识别，避免误伤
            b64_source = candidate

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
                # flowbot 硬上限 5MB，所有来源 >5MB 拒绝，直接跳过
                logger.warning(
                    f"FlowBot 图片 {size} 字节超过 5MB 上限，已跳过 "
                    f"(session={session_id})"
                )
                return
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
        downloaded_path = ""
        for candidate in (raw, url, comp_path):
            if candidate and os.path.isfile(candidate):
                local_path = candidate
                break
        if not local_path and direct_url:
            # 无论是否透传 URL，都先下载一份本地缓存，供 image_url 发送失败时回退 base64
            local_path = await self._download_image(direct_url)
            if local_path:
                downloaded_path = local_path
            if use_direct_url and not local_path:
                logger.debug("FlowBot 图片兜底下载失败，继续透传 URL")

        try:
            if use_direct_url and direct_url:
                payload["image_url"] = direct_url
            elif local_path:
                size = os.path.getsize(local_path)
                if size > threshold_bytes:
                    # flowbot 硬上限 5MB（URL 同限），直接跳过
                    logger.warning(
                        f"FlowBot 图片 {size} 字节超过 5MB 上限，已跳过 "
                        f"(session={session_id})"
                    )
                    return
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
                logger.warning(
                    f"FlowBot image_url 发送失败，回退 image_base64: {session_id}"
                )
                retry: dict = {"session_id": session_id, "type": "image"}
                if reply_to:
                    retry["reply_to"] = reply_to
                try:
                    with open(local_path, "rb") as f:
                        retry["image_base64"] = base64.b64encode(f.read()).decode(
                            "ascii"
                        )
                except Exception as e:
                    logger.warning(f"FlowBot 图片 base64 回退读取失败: {e}")
                    return
                retry["image_path"] = local_path
                result = await self._api_json("POST", "/api/v1/messages/send", retry)
            if result is not None:
                self._stats["sent"] += 1
                logger.info(f"FlowBot image -> {session_id}")
        finally:
            # 随用随清：删除本次下载的临时缓存文件
            if downloaded_path:
                try:
                    if os.path.isfile(downloaded_path):
                        os.remove(downloaded_path)
                except OSError:
                    pass
                self._forget_temp_file(downloaded_path)

    @staticmethod
    def _extract_base64(source: str) -> str:
        """从图片/视频源提取裸 base64 串。

        支持 `base64://`、`data:image/...;base64,`、以及裸 base64（经
        `_looks_like_base64` 保守判定，避免误伤路径/URL）。非 base64 源返回空串。
        """
        candidate = str(source or "").strip()
        if not candidate:
            return ""
        if candidate.startswith("base64://"):
            return candidate[len("base64://"):].strip()
        if candidate.startswith("data:") and "," in candidate:
            return candidate.split(",", 1)[-1].strip()
        if _looks_like_base64(candidate):
            return candidate.strip()
        return ""

    def _normalize_video_url(self, url: str) -> str:
        """视频直链归一：仿图片 `_fix_image_url`，将容器内 127.0.0.1 地址改写为宿主机可达。

        非 http(s) 源返回空串（表示无可用直链）。
        """
        candidate = str(url or "").strip()
        if not candidate:
            return ""
        if not candidate.startswith("http"):
            return ""
        return self._fix_image_url(candidate)

    async def _send_video(self, session_id: str, comp) -> None:
        """发送视频/文件组件：本地文件走 upload token，直链走 video_url，
        base64 源 ≤ 阈值内联；失败仅告警，不炸消息循环。"""
        try:
            def _pick(candidates):
                for c in candidates:
                    if c:
                        return c
                return ""

            def _strip_file_uri(val: str) -> str:
                if val.startswith("file:///"):
                    return val[len("file:///"):].strip()
                return val

            comp_file = str(getattr(comp, "file", None) or "").strip()
            comp_url = str(getattr(comp, "url", None) or "").strip()
            comp_path = str(getattr(comp, "path", None) or "").strip()

            # 死路径防御：剥 file:/// 前缀；本地文件存在时才读作真实来源
            stripped = [_strip_file_uri(v) for v in (comp_file, comp_url, comp_path)]
            _fetch = []
            for s in stripped:
                if s.startswith("http") or _looks_like_base64(s):
                    _fetch.append(s)
                elif s.startswith("base64://") or (s.startswith("data:") and "," in s):
                    _fetch.append(s)
                elif s and os.path.isfile(s):
                    _fetch.append(s)
            source = _pick(tuple(_fetch))
            if not source:
                logger.warning(
                    "[FlowBot] 视频组件无可用来源，已忽略 (session=%s)", session_id
                )
                return

            payload: dict = {"session_id": session_id, "type": "video"}

            try:
                threshold_mb = max(
                    1, int(self.config.get("flowbot_video_size_threshold", 14) or 14)
                )
            except (TypeError, ValueError):
                threshold_mb = 14
            threshold_bytes = threshold_mb * 1024 * 1024

            is_existing_file = source and not source.startswith("http") and (
                os.path.isfile(source)
            )

            if is_existing_file:
                # 本地文件视频：upload token 通道（统一大文件/小文件链路）。
                # 上传后以 media_path 引用 flowbot 侧落盘产物（/tmp/weflow_uploads/<token><ext>）。
                try:
                    up = await self._upload_media(
                        local_path=source, kind="video"
                    )
                except Exception as e:
                    logger.warning(
                        f"[FlowBot] 视频上传异常 (session={session_id}): {e}"
                    )
                    up = None
                if not up:
                    logger.warning(
                        "[FlowBot] 视频上传失败或无 token，已忽略 "
                        "(session=%s, file=%s)",
                        session_id, source,
                    )
                    return
                token = str(up.get("token") or "").strip()
                up_path = str(up.get("path") or "").strip()
                if up_path:
                    media_path = up_path
                elif token:
                    ext = ""
                    lower_src = source.lower()
                    for cand_ext in (".mp4", ".mkv", ".webm", ".mov", ".flv"):
                        if lower_src.endswith(cand_ext):
                            ext = cand_ext
                            break
                    media_path = f"/tmp/weflow_uploads/{token}{ext}"
                else:
                    logger.warning(
                        "[FlowBot] 视频上传响应无 token/path，已忽略 "
                        "(session=%s)", session_id,
                    )
                    return
                payload["media_path"] = media_path
            elif source.startswith("http"):
                # http(s) 直链：video_url，由 FlowBot 自行下载
                payload["video_url"] = self._normalize_video_url(source)
            else:
                # base64 源（base64:// / data: / 裸 base64）：≤阈值内联
                b64 = self._extract_base64(source)
                if b64 and len(b64) * 3 // 4 <= threshold_bytes:
                    payload["video_base64"] = b64
                else:
                    logger.warning(
                        "[FlowBot] 视频发送跳过: base64 源超阈值(%dMB) "
                        "且无 URL/本地文件 (session=%s)",
                        threshold_mb, session_id,
                    )
                    return

            await self._api_json("POST", "/api/v1/messages/send", payload)
            self._stats["sent"] += 1
            logger.info(f"FlowBot video -> {session_id}")
        except Exception as e:
            logger.warning("[FlowBot] 视频发送失败（FlowBot 版本过低或网络异常）: %s", e)

    async def _upload_media(
        self,
        local_path: str | None = None,
        b64: str | None = None,
        kind: str = "image",
        source_url: str | None = None,
    ) -> dict | None:
        """上传媒体到 /api/v1/media/upload（仅 JSON body），成功返回
        {"token", "path"} 供 media_path 引用；失败返回 None。来源三选一：
        local_path 读文件转 base64 / b64 直传 / source_url 直链。"""
        if not b64 and not source_url:
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
            payload: dict = {}
            if source_url:
                payload[f"{kind}_url"] = source_url
            else:
                payload[f"{kind}_base64"] = b64
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
                        token = ""
                        path = ""
                        for k in ("token", "image_token", "file_token", "media_id"):
                            if container.get(k):
                                token = str(container[k])
                                break
                        for k in ("path", "filePath", "mediaPath", "url"):
                            if container.get(k):
                                path = str(container[k])
                                break
                        if token or path:
                            return {"token": token, "path": path}
                    if isinstance(inner, str) and inner:
                        return {"token": inner, "path": ""}
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"FlowBot 媒体上传异常: {e}")
            return None

    # ── 会话缓存（仅由消息驱动更新，无定时轮询） ──────────────────────────


class FlowBotMention(BaseMessageComponent):
    """FlowBot 自研 @ 组件：wxid 语义，不依赖 OneBot 的 qq 字段。

    type 复用 At 以兼容 AstrBot 序列化；toDict 把 wxid 映射进 qq 位置，
    保证 AstrBot 内部重建 At(qq=...) 不报错。
    """

    type: ComponentType = ComponentType.At
    wxid: str = ""
    name: str | None = ""

    def __init__(self, wxid: str = "", name: str = "", **_) -> None:
        super().__init__(wxid=wxid, name=name)

    def toDict(self):
        return {"type": "at", "data": {"qq": str(self.wxid)}}


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
        # 群消息回复目标应为群会话（get_session_id），而非发送者本人（get_sender_id）
        target = self.get_session_id() or self.get_sender_id()
        await self._platform._send_to_session(target, message)
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


def _looks_like_base64(s: str) -> bool:
    """判断字符串是否像裸 base64 图片数据（无 base64:// 前缀）。

    仅当长度足够、不含路径/URL 特征、字符集合法时才判定，避免误伤本地路径。
    """
    s = s.strip()
    if len(s) < _BARE_B64_MIN_LEN:
        return False
    if "://" in s or "/" in s or os.sep in s:
        return False
    if not s or len(s) % 4 != 0:
        return False
    return all(c in _B64_CHARSET for c in s)


def _wxid_match(a: str, b: str) -> bool:
    """归一化比较微信 wxid，容忍「wxid_」前缀与大小写差异。

    FlowBot `/api/v1/group-members` 返回的 `wxid` 恒带 `wxid_` 前缀
    （如 wxid_kbwhoagqmspj21），而消息事件侧的 sender_id 可能带/去前缀
    （kbwhoagqmspj21 或 wxid_kbwhoagqmspj21）。严格相等会导致头像/昵称
    匹配失败、退化为显示原始 wxid。此处剥离前缀并用小写比较，使两种形态互认。
    """
    na = str(a or "").strip().lower()
    nb = str(b or "").strip().lower()
    if na.startswith("wxid_"):
        na = na[len("wxid_"):]
    if nb.startswith("wxid_"):
        nb = nb[len("wxid_"):]
    return bool(na) and na == nb


def _mask_host(base: str) -> str:
    """隐藏日志中的 host 细节：去掉协议前缀并打码主机部分，仅保留端口示意。"""
    cleaned = base.replace("http://", "").replace("https://", "")
    if ":" in cleaned:
        host, port = cleaned.rsplit(":", 1)
        if host:
            first = host.split(".")[0]
            masked = f"{first}.***"
        else:
            masked = "***"
        return f"{masked}:{port}"
    return cleaned
