"""aiohttp 服务：北向 /v1/chat/completions、/v1/models，南向与回退客户端装配，WebUI。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .appkeys import (
    KEY_CONFIG_PATH,
    KEY_FALLBACK,
    KEY_RUNTIME,
    KEY_SESSION,
    KEY_STATS,
    KEY_SYSTEMONE,
    RuntimeState,
)
from .config import BridgeConfig, bootstrap_config_file, load_config
from .fallback import FallbackError, FallbackClient
from .logging_setup import setup_logging
from .northbound import RoutingResult
from .stats import BridgeStats
from .systemone import SystemOneClient
from .webui import build_router, register_webui

logger = logging.getLogger("systemone_bridge.server")

# 静态对外模型清单（v1：两个规格都展示，实际权重由 SystemOne 端侧决定）
ADVERTISED_MODELS = ("intern-decision-4b", "intern-decision-0.8b")


@asynccontextmanager
async def create_app(
    config: BridgeConfig, project_root: Path, config_path: Path | None = None
):
    setup_logging(config.logging.level, project_root / config.logging.dir)
    session = aiohttp.ClientSession()
    try:
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app[KEY_SESSION] = session
        app[KEY_SYSTEMONE] = SystemOneClient(config.upstream_systemone, session)
        app[KEY_FALLBACK] = FallbackClient(config.fallback, session)
        app[KEY_STATS] = BridgeStats()
        if config_path is not None:
            app[KEY_CONFIG_PATH] = config_path
        # 解析器装配与 WebUI 保存后的热应用共用同一构建逻辑；
        # config/router 收进启动前注册的 RuntimeState，保存热应用只换其内部引用
        router = build_router(config, app[KEY_SYSTEMONE], app[KEY_FALLBACK], app[KEY_STATS])
        app[KEY_RUNTIME] = RuntimeState(config=config, router=router)

        app.router.add_post("/v1/chat/completions", handle_chat_completions)
        app.router.add_get("/v1/models", handle_models)
        app.router.add_get("/v1/models/", handle_models)
        app.router.add_get("/health", handle_health)
        register_webui(app)
        yield app
    finally:
        await session.close()


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": {"message": "body must be a JSON object"}}, status=400)
    router = request.app[KEY_RUNTIME].router
    try:
        result = await router.handle_chat_completions(body)
    except FallbackError as exc:
        logger.error("回退上游失败：%s", exc)
        return web.json_response(
            {"error": {"message": f"fallback upstream failed: {exc}"}}, status=502
        )
    return await _render(request, result)


async def _render(request: web.Request, result: RoutingResult) -> web.StreamResponse:
    if result.payload is not None:
        return web.json_response(result.payload)
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-store",
        },
    )
    await response.prepare(request)
    try:
        async for chunk in result.stream:
            await response.write(chunk)
        await response.write_eof()
    except (ConnectionResetError, FallbackError) as exc:
        if isinstance(exc, FallbackError):
            logger.error("流式回退中断：%s", exc)
            # 响应头已发出，无法再改状态码；尽力截断流
            with contextlib.suppress(Exception):
                await response.write_eof()
    return response


async def handle_models(request: web.Request) -> web.Response:
    data = [
        {"id": name, "object": "model", "owned_by": "systemone-bridge"}
        for name in ADVERTISED_MODELS
    ]
    return web.json_response({"object": "list", "data": data})


async def handle_health(request: web.Request) -> web.Response:
    systemone: SystemOneClient = request.app[KEY_SYSTEMONE]
    upstream = await systemone.health()
    return web.json_response(
        {
            "status": "ok",
            "version": __version__,
            "systemone_upstream": upstream or {"reachable": False},
        }
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="systemone-bridge 本地协议桥")
    parser.add_argument(
        "--config",
        default="config.toml",
        help="配置文件路径（默认项目根 config.toml，缺省时用内置默认值）",
    )
    parser.add_argument("--host", default=None, help="覆盖配置中的 host")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置中的 port")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(args.config)
    # 首启 bootstrap：config.toml 不存在时从模板生成（可开箱后到 WebUI 填密钥）
    bootstrap_config_file(config_path, project_root / "config.toml.example")
    config = load_config(config_path)
    host = args.host or config.server.host
    port = args.port or config.server.port

    async def run() -> None:
        async with create_app(config, project_root, config_path) as app:
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
            logger.info("systemone-bridge v%s 监听 http://%s:%d", __version__, host, port)
            while True:
                await asyncio.sleep(3600)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
