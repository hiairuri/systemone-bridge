"""aiohttp 应用状态键集中定义（server 与 webui 共用，避免循环导入）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web

from .config import BridgeConfig
from .fallback import FallbackClient
from .northbound import NorthboundRouter
from .stats import BridgeStats
from .systemone import SystemOneClient

KEY_SESSION = web.AppKey("session", aiohttp.ClientSession)
KEY_SYSTEMONE = web.AppKey("systemone", SystemOneClient)
KEY_FALLBACK = web.AppKey("fallback", FallbackClient)
KEY_STATS = web.AppKey("stats", BridgeStats)
KEY_CONFIG_PATH = web.AppKey("config_path", Path)


@dataclass
class RuntimeState:
    """可热替换的运行时组件持有者。

    在应用启动前注册进 Application；WebUI 保存后原地换内部引用（而非改写
    已启动的 Application 状态，那会触发 aiohttp 的弃用警告）。
    """

    config: BridgeConfig
    router: NorthboundRouter


KEY_RUNTIME = web.AppKey("runtime", RuntimeState)
