"""Connect AstrBot commands to the separately installed Big Fat Fish home."""

import asyncio
import base64
import binascii
import json
import re
import uuid

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_SIZE = 8 * 1024 * 1024
MAX_JSON_SIZE = MAX_PNG_SIZE * 4 // 3 + 8192


class HomeUnavailable(Exception):
    pass


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client: aiohttp.ClientSession | None = None
        self.command_lock = asyncio.Lock()

    async def initialize(self):
        self.client = aiohttp.ClientSession(trust_env=False)

    async def terminate(self):
        if self.client is not None:
            await self.client.close()

    def configured_link(self):
        port = self.config.get("home_port", 3218)
        token = self.config.get("home_token", "")
        if type(port) is not int or not 1 <= port <= 65535:
            raise HomeUnavailable("小屋端口无效，请在 AstrBot 面板核对插件配置。")
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            raise HomeUnavailable("请在 AstrBot 面板填写小屋的联动令牌。")
        return port, token

    async def request_home(self, link, action, body, timeout=3):
        port, token = link
        try:
            async with self.client.post(
                f"http://127.0.0.1:{port}/api/qq-link/{action}",
                headers={"x-fatfish-link": token},
                json=body,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as result:
                if result.status in {401, 403}:
                    raise HomeUnavailable("小屋联动未授权，请核对插件里的令牌和端口。")
                if result.status != 200:
                    raise HomeUnavailable("小屋暂时无法处理这个请求。")
                data = bytearray()
                async for chunk in result.content.iter_chunked(65536):
                    if len(data) + len(chunk) > MAX_JSON_SIZE:
                        raise HomeUnavailable("小屋返回的画面过大。")
                    data.extend(chunk)
                value = json.loads(data)
                if not isinstance(value, dict) or value.get("ok") is not True:
                    raise HomeUnavailable("小屋返回的状态无效。")
                return value
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise HomeUnavailable("暂时连不上小屋，确认小屋正在同一台机器运行。") from exc

    async def observe(self, link, screenshot):
        response = await self.request_home(link, "observe", {"screenshot": screenshot}, timeout=32 if screenshot else 3)
        observation = response.get("observation")
        if not isinstance(observation, dict) or observation.get("version") != 1:
            raise HomeUnavailable("小屋版本不兼容，请升级公开版小屋。")
        description = observation.get("description")
        if not isinstance(description, str) or not 0 < len(description) <= 300:
            raise HomeUnavailable("小屋暂时没有可显示的活动状态。")
        if not screenshot:
            return description, None
        data = response.get("png")
        try:
            png = base64.b64decode(data, validate=True) if isinstance(data, str) else b""
        except (binascii.Error, ValueError) as exc:
            raise HomeUnavailable("小屋返回的截图无效。") from exc
        if not png.startswith(PNG_SIGNATURE) or len(png) > MAX_PNG_SIZE:
            raise HomeUnavailable("小屋这次没有返回可用的截图。")
        return description, png

    def is_owner(self, event):
        owner = self.config.get("owner_qq", "")
        return bool(
            isinstance(owner, str)
            and re.fullmatch(r"[1-9][0-9]{4,14}", owner)
            and event.get_platform_name() == "aiocqhttp"
            and event.is_private_chat()
            and str(event.get_sender_id()) == owner
        )

    async def reply_from_device(self, event, link, screenshot):
        description, png = await self.observe(link, screenshot)
        request_id = uuid.uuid4().hex
        sent = False
        try:
            ready = await self.request_home(link, "acquire", {"id": request_id})
            async with asyncio.timeout(45):
                while ready.get("ready") is not True:
                    await asyncio.sleep(0.3)
                    ready = await self.request_home(link, "heartbeat", {"id": request_id})
            if self.is_owner(event):
                chain = [Plain(description)]
                if png is not None:
                    chain.append(Image.fromBytes(png))
                await event.send(MessageChain(chain=chain))
                sent = True
        except TimeoutError as exc:
            raise HomeUnavailable("肥鱼还没走到设备前，等会儿再试试。") from exc
        finally:
            try:
                await self.request_home(link, "release", {"id": request_id, "sent": sent})
            except HomeUnavailable:
                logger.debug("大肥鱼小屋设备占用将在到期后释放。")

    async def respond(self, event, screenshot):
        event.stop_event()
        if not self.is_owner(event):
            return
        if self.command_lock.locked():
            await event.send(MessageChain().message("上一条小屋请求还在处理。"))
            return
        async with self.command_lock:
            try:
                await self.reply_from_device(event, self.configured_link(), screenshot)
            except HomeUnavailable as exc:
                if self.is_owner(event):
                    await event.send(MessageChain().message(str(exc)))

    @filter.command("肥鱼小屋")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def home_status(self, event: AstrMessageEvent):
        """查看大肥鱼小屋当前活动。"""
        await self.respond(event, screenshot=False)

    @filter.command("肥鱼截图")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def home_screenshot(self, event: AstrMessageEvent):
        """取得当前大肥鱼小屋场景截图。"""
        await self.respond(event, screenshot=True)
