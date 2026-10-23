"""WebUI 后端：配置/状态/连通性测试 API 与首页静态页。

安全基线（本地服务，默认只绑 127.0.0.1）：
- 任何 GET 响应中的 api_key 一律脱敏为「前 4 字符 + ***」；
- POST 保存时 api_key 若仍是掩码占位（以 *** 结尾）则保留旧值；
- 保存先 pydantic 校验，再原子写盘（临时文件 + replace），最后热应用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, TypeVar

import aiohttp
from aiohttp import web
from pydantic import BaseModel, ValidationError

from .appkeys import (
    KEY_CONFIG_PATH,
    KEY_FALLBACK,
    KEY_RUNTIME,
    KEY_SESSION,
    KEY_STATS,
    KEY_SYSTEMONE,
)
from .config import BridgeConfig, UpstreamSystemOneConfig, dump_config_toml
from .northbound import NorthboundRouter

logger = logging.getLogger("systemone_bridge.webui")

# 掩码占位后缀：GET 返回与 POST 识别共用；以此结尾的提交值视为"未修改"
MASK_SUFFIX = "***"

# 脱敏字段 → 所属配置段（POST 保存时做掩码保留）
_SECRET_FIELDS: dict[str, str] = {
    "upstream_systemone": "api_key",
    "fallback": "api_key",
}


def mask_secret(value: str) -> str:
    """密钥脱敏：前 4 字符 + ***；过短密钥只留首字符，空值保持空。"""

    if not value:
        return ""
    if len(value) <= 4:
        return value[:1] + MASK_SUFFIX
    return value[:4] + MASK_SUFFIX


def is_mask_placeholder(value: str) -> bool:
    return value.endswith(MASK_SUFFIX)


def masked_config(config: BridgeConfig) -> dict[str, Any]:
    """配置快照（GET /api/config 用）：api_key 字段脱敏，其余原样。"""

    data = config.model_dump()
    for section, field in _SECRET_FIELDS.items():
        section_data = data.get(section)
        if isinstance(section_data, dict):
            section_data[field] = mask_secret(str(section_data.get(field) or ""))
    return data


def _sanitize_validation_error(exc: ValidationError) -> list[dict[str, Any]]:
    """校验错误瘦身：只留位置/消息/类型，避免把提交的密钥回显给客户端或日志。"""

    return [
        {"loc": list(error.get("loc") or ()), "msg": error.get("msg"), "type": error.get("type")}
        for error in exc.errors()
    ]


# ---------- 配置读写与热应用 ----------


def merge_submitted_config(current: BridgeConfig, body: dict[str, Any]) -> dict[str, Any]:
    """按段合并提交体到当前配置（客户端只会上送完整配置，此处是防御：缺段不重置）。"""

    merged = current.model_dump()
    for key, value in body.items():
        if key in merged:
            merged[key] = value
    # 掩码占位保留旧值：提交值以 *** 结尾 → 换回当前真实密钥
    for section, field in _SECRET_FIELDS.items():
        section_data = merged.get(section)
        if not isinstance(section_data, dict):
            continue
        submitted = str(section_data.get(field) or "")
        if is_mask_placeholder(submitted):
            section_data[field] = getattr(getattr(current, section), field)
    return merged


def atomic_write_config(path: Any, config: BridgeConfig) -> None:
    """原子写盘：先写同目录临时文件，再 os.replace 覆盖目标。"""

    text = dump_config_toml(config)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def build_router(config: BridgeConfig, systemone: Any, fallback: Any, stats: Any) -> NorthboundRouter:
    """按当前配置装配解析器与北向路由器（启动与热应用共用）。"""

    from .parsers import DfCheckPlannerParser, ExpressionSelectorParser

    parsers: list[Any] = []
    if config.task_parsers.expression_selector:
        parsers.append(
            ExpressionSelectorParser(
                min_probability=config.translation.expression_min_probability,
                max_selected=config.translation.expression_max_selected,
            )
        )
    if config.task_parsers.df_check_planner:
        parsers.append(DfCheckPlannerParser())
    return NorthboundRouter(parsers, systemone, fallback, stats=stats)


def apply_hot_config(app: web.Application, new_config: BridgeConfig) -> list[str]:
    """把已校验的新配置热应用到运行中的桥（对后续请求立即生效，不重启）。

    返回提示列表（如 server 段变更需重启）。磁盘写入由调用方先行完成。
    """

    notes: list[str] = []
    runtime = app[KEY_RUNTIME]
    old_config: BridgeConfig = runtime.config
    runtime.config = new_config

    # 上游客户端原位换配置：URL/密钥/超时在每次请求时读取，对后续请求即时生效
    app[KEY_SYSTEMONE].update_config(new_config.upstream_systemone)
    app[KEY_FALLBACK].update_config(new_config.fallback)

    # 解析器/翻译参数在路由器装配时定格 → 重建路由器（共享统计器）
    runtime.router = build_router(new_config, app[KEY_SYSTEMONE], app[KEY_FALLBACK], app[KEY_STATS])

    # 日志级别可热调（root systemone_bridge logger）
    if new_config.logging.level != old_config.logging.level:
        logging.getLogger("systemone_bridge").setLevel(new_config.logging.level)
        notes.append(f"日志级别已切换为 {new_config.logging.level}")

    if (
        new_config.server.host != old_config.server.host
        or new_config.server.port != old_config.server.port
    ):
        notes.append("server.host/port 变更需重启服务后生效")
    if new_config.logging.dir != old_config.logging.dir:
        notes.append("logging.dir 变更需重启服务后生效")
    return notes


# ---------- API 处理器 ----------


async def handle_get_config(request: web.Request) -> web.Response:
    config: BridgeConfig = request.app[KEY_RUNTIME].config
    return web.json_response(masked_config(config))


async def handle_post_config(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)

    current: BridgeConfig = request.app[KEY_RUNTIME].config
    merged = merge_submitted_config(current, body)
    try:
        new_config = BridgeConfig.model_validate(merged)
    except ValidationError as exc:
        return web.json_response(
            {"error": "配置校验失败，未保存", "details": _sanitize_validation_error(exc)},
            status=400,
        )

    config_path = request.app.get(KEY_CONFIG_PATH)
    if config_path is not None:
        try:
            atomic_write_config(config_path, new_config)
        except OSError as exc:
            logger.error("配置写盘失败：%s", exc)
            return web.json_response({"error": f"写盘失败，未保存：{exc}"}, status=500)

    notes = apply_hot_config(request.app, new_config)
    logger.info("WebUI 保存配置并热应用%s", "（含写盘）" if config_path is not None else "（未指定配置路径，仅热应用）")
    return web.json_response(
        {"ok": True, "notes": notes, "config": masked_config(new_config)}
    )


async def handle_status(request: web.Request) -> web.Response:
    stats = request.app[KEY_STATS]
    from . import __version__

    payload = stats.snapshot(recent_limit=10)
    payload["version"] = __version__
    payload["server"] = f"{request.app[KEY_RUNTIME].config.server.host}:{request.app[KEY_RUNTIME].config.server.port}"
    return web.json_response(payload)


async def handle_index(request: web.Request) -> web.StreamResponse:
    """返回 WebUI 单页（static/index.html）。"""

    from pathlib import Path

    index_path = Path(__file__).resolve().parents[2] / "static" / "index.html"
    if not index_path.is_file():
        return web.json_response({"error": "static/index.html 缺失"}, status=404)
    return web.FileResponse(index_path, headers={"Cache-Control": "no-store"})


# ---------- 连通性测试端点 ----------

_SectionT = TypeVar("_SectionT", bound=BaseModel)

# 测试端点允许的覆盖字段（api_key 掩码/缺省时沿用当前配置）
_TEST_OVERRIDE_FIELDS: dict[str, tuple[str, ...]] = {
    "systemone": ("base_url", "decisions_path", "api_key", "model", "timeout_seconds"),
    "fallback": ("base_url", "api_key", "model", "timeout_seconds"),
}


async def _resolve_test_section(
    request: web.Request, kind: str, section_type: type[_SectionT]
) -> _SectionT | None:
    """组装测试用配置段：默认当前配置，允许请求体按字段覆盖。非法请求返回 None。"""

    current = request.app[KEY_RUNTIME].config
    base = getattr(current, "upstream_systemone" if kind == "systemone" else "fallback")
    overrides: dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(body, dict):
            return None
        allowed = _TEST_OVERRIDE_FIELDS[kind]
        overrides = {key: value for key, value in body.items() if key in allowed}
    data = base.model_dump()
    data.update(overrides)
    try:
        return section_type.model_validate(data)
    except ValidationError:
        return None


async def probe_systemone(section: UpstreamSystemOneConfig, session: aiohttp.ClientSession) -> dict[str, Any]:
    """真实探测 SystemOne 上游：GET {base_url}/health，返回延迟与错误分类。"""

    url = section.base_url.rstrip("/") + "/health"
    headers = {"Authorization": f"Bearer {section.api_key}"} if section.api_key else {}
    timeout = aiohttp.ClientTimeout(total=min(float(section.timeout_seconds) or 5.0, 10.0))
    start = time.perf_counter()
    try:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            body_text = (await response.text())[:200]
            if response.status == 200:
                return {"ok": True, "category": "ok", "status": 200, "latency_ms": latency_ms, "detail": body_text}
            if response.status == 404:
                return {
                    "ok": False,
                    "category": "no_health_endpoint",
                    "status": 404,
                    "latency_ms": latency_ms,
                    "detail": "端点可达但无 /health（/v1/decisions 可能仍可用）",
                }
            category = "server_error" if response.status >= 500 else "rejected"
            return {
                "ok": False,
                "category": category,
                "status": response.status,
                "latency_ms": latency_ms,
                "detail": body_text,
            }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "category": "unreachable",
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "detail": f"超时（>{timeout.total}s）",
        }
    except aiohttp.ClientError as exc:
        return {
            "ok": False,
            "category": "unreachable",
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "detail": str(exc),
        }


async def probe_fallback(section: Any, session: aiohttp.ClientSession) -> dict[str, Any]:
    """真实探测回退上游：发一次最小 chat completion（max_tokens=1），验证 URL/密钥/模型。"""

    url = section.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if section.api_key:
        headers["Authorization"] = f"Bearer {section.api_key}"
    payload = {
        "model": section.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    timeout = aiohttp.ClientTimeout(total=min(float(section.timeout_seconds) or 15.0, 30.0))
    start = time.perf_counter()
    try:
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            body_text = (await response.text())[:200]
            if response.status == 200:
                model = ""
                try:
                    model = str(json.loads(body_text).get("model") or "")
                except json.JSONDecodeError:
                    pass
                return {
                    "ok": True,
                    "category": "ok",
                    "status": 200,
                    "latency_ms": latency_ms,
                    "detail": f"模型 {model or section.model} 可用",
                }
            if response.status in (401, 403):
                category = "auth_error"
            elif response.status >= 500:
                category = "server_error"
            else:
                category = "rejected"
            return {
                "ok": False,
                "category": category,
                "status": response.status,
                "latency_ms": latency_ms,
                "detail": body_text,
            }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "category": "unreachable",
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "detail": f"超时（>{timeout.total}s）",
        }
    except aiohttp.ClientError as exc:
        return {
            "ok": False,
            "category": "unreachable",
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "detail": str(exc),
        }


async def handle_test_systemone(request: web.Request) -> web.Response:
    section = await _resolve_test_section(request, "systemone", UpstreamSystemOneConfig)
    if section is None:
        return web.json_response({"error": "测试参数非法"}, status=400)
    result = await probe_systemone(section, request.app[KEY_SESSION])
    return web.json_response(result)


async def handle_test_fallback(request: web.Request) -> web.Response:
    from .config import FallbackConfig

    section = await _resolve_test_section(request, "fallback", FallbackConfig)
    if section is None:
        return web.json_response({"error": "测试参数非法"}, status=400)
    result = await probe_fallback(section, request.app[KEY_SESSION])
    return web.json_response(result)


def register_webui(app: web.Application) -> None:
    """把 WebUI 路由挂到应用上（由 create_app 调用）。"""

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_post("/api/config", handle_post_config)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/test/systemone", handle_test_systemone)
    app.router.add_post("/api/test/fallback", handle_test_fallback)
