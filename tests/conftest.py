"""测试公共设施：真实快照重建北向请求、DF 请求构建器、三件套测试栈。"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from aiohttp import ClientSession, web

TESTS_DIR = Path(__file__).parent
FIXTURES = TESTS_DIR / "fixtures"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from mock_systemone import create_mock_systemone_app, spawn  # noqa: E402

from systemone_bridge.config import (  # noqa: E402
    BridgeConfig,
    FallbackConfig,
    LoggingConfig,
    ServerConfig,
    TaskParsersConfig,
    TranslationConfig,
    UpstreamSystemOneConfig,
)
from systemone_bridge.server import create_app  # noqa: E402


# ---------- exprsel：真实快照 → 北向 messages ----------


def load_snapshot(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as stream:
        return json.load(stream)


def snapshot_to_messages(raw: dict[str, Any]) -> list[dict[str, str]]:
    """把 MaiBot 快照的 request_items 还原为 OpenAI messages 形状。"""

    messages: list[dict[str, str]] = []
    for item in raw["request_items"]:
        kind = item.get("item_type")
        if kind == "SystemMessageItem":
            role = "system"
        elif kind == "UserMessageItem":
            role = "user"
        else:
            continue
        text = "\n".join(
            str(part.get("text") or "") for part in item.get("parts") or [] if isinstance(part, dict)
        )
        messages.append({"role": role, "content": text})
    return messages


def exprsel_request(name: str, model: str = "intern-decision-4b", **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "messages": snapshot_to_messages(load_snapshot(name)),
        **extra,
    }


# ---------- DF 检定：按 check_planner.py 实测形状构建请求 ----------

DEFAULT_ATTRIBUTES = [
    {"key": key, "name": name}
    for key, name in [
        ("strength", "力量"), ("dexterity", "敏捷"), ("constitution", "体质"),
        ("intelligence", "智力"), ("wisdom", "感知"), ("charisma", "魅力"),
    ]
]
DEFAULT_DC_TABLE = {"easy": 10, "normal": 15, "hard": 20, "extreme": 25}


def make_df_player(
    pid: str = "u1",
    action: str = "我在门口听动静",
    skills: list[dict[str, Any]] | None = None,
    selected_attribute: str = "",
    selected_skill: str = "",
) -> dict[str, Any]:
    return {
        "player_id": pid,
        "character_name": f"角色{pid}",
        "action": action,
        "attributes": {"力量": 12, "敏捷": 14},
        "skills": skills if skills is not None else [{"name": "侦查", "value": 40}, {"name": "潜行", "value": 35}],
        "selected_attribute": selected_attribute,
        "selected_skill": selected_skill,
        "target_text": "",
        "item_context": {"items": [], "partial": False},
    }


def build_df_request(
    players: list[dict[str, Any]],
    dice_system: str = "d20",
    dc_table: dict[str, Any] | None = None,
    attributes: list[dict[str, Any]] | None = None,
    max_check_dc: int | None = None,
    model: str = "intern-decision-4b",
) -> dict[str, Any]:
    system = (FIXTURES / "check_planner_zh.md").read_text(encoding="utf-8")
    ruleset: dict[str, Any] = {
        "id": "base_d20" if dice_system == "d20" else "coc7",
        "dice_system": dice_system,
        "mechanic": {"dice": dice_system},
        "attributes": DEFAULT_ATTRIBUTES if attributes is None else attributes,
        "dc_table": DEFAULT_DC_TABLE if dc_table is None else dc_table,
        "target_policy": (
            "server_uses_character_sheet_percentile"
            if dice_system == "d100"
            else "gm_supplies_situational_dc"
        ),
        "currency_units": [],
        "currency_display_unit": "gold",
    }
    if max_check_dc is not None:
        ruleset["max_check_dc"] = max_check_dc
    payload = {
        "round": 3,
        "scene": "雨夜酒馆，炉火将熄。",
        "recent_narration": ["酒保把一杯麦酒推到你面前，眼神闪躲。"],
        "difficulty": "normal",
        "ruleset": ruleset,
        "players": players,
        "companions": [],
        "recent_purchases": [],
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "dice_checks",
                    "description": "Inspect the complete batch of player actions...",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }


# ---------- mock 回退上游 ----------


def default_completion(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fallback0",
        "object": "chat.completion",
        "created": 1700000000,
        "model": body.get("model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fallback-ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def create_mock_fallback_app(
    calls: list[dict[str, Any]],
    *,
    status: int = 200,
    body: dict[str, Any] | None = None,
    sse_chunks: list[bytes] | None = None,
) -> web.Application:
    async def chat(request: web.Request) -> web.StreamResponse:
        calls.append(await request.json())
        if status != 200:
            return web.json_response({"error": {"message": "upstream boom"}}, status=status)
        if sse_chunks is not None:
            response = web.StreamResponse(
                headers={"Content-Type": "text/event-stream; charset=utf-8"}
            )
            await response.prepare(request)
            for chunk in sse_chunks:
                await response.write(chunk)
            await response.write_eof()
            return response
        return web.json_response(body if body is not None else default_completion(calls[-1]))

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


# ---------- 三件套测试栈 ----------


@dataclass
class Stack:
    bridge_url: str
    systemone_url: str
    fallback_url: str
    systemone_received: list[dict[str, Any]] = field(default_factory=list)
    fallback_calls: list[dict[str, Any]] = field(default_factory=list)
    _runners: list[web.AppRunner] = field(default_factory=list)

    async def aclose(self) -> None:
        for runner in self._runners:
            await runner.cleanup()


def make_stack_config(systemone_url: str, fallback_base: str, **overrides: Any) -> BridgeConfig:
    fields: dict[str, Any] = {
        "server": ServerConfig(),
        "upstream_systemone": UpstreamSystemOneConfig(base_url=systemone_url, timeout_seconds=5),
        "fallback": FallbackConfig(
            base_url=fallback_base, api_key="test-key", model="GLM-TEST", timeout_seconds=15
        ),
        "task_parsers": TaskParsersConfig(),
        "translation": TranslationConfig(),
        "logging": LoggingConfig(level="WARNING", dir="logs-test"),
    }
    fields.update(overrides)
    return BridgeConfig.model_validate(fields)


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    received: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def capture_handler(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        from mock_systemone import default_handler

        return default_handler(request)

    systemone_url, runner1 = await spawn(create_mock_systemone_app(capture_handler))
    fallback_url, runner2 = await spawn(create_mock_fallback_app(calls))

    config = make_stack_config(systemone_url, fallback_url + "/v1")
    async with create_app(config, tmp_path) as bridge_app:
        bridge_url, runner3 = await spawn(bridge_app)
        yield Stack(
            bridge_url=bridge_url,
            systemone_url=systemone_url,
            fallback_url=fallback_url,
            systemone_received=received,
            fallback_calls=calls,
            _runners=[runner1, runner2, runner3],
        )


@pytest.fixture
async def http_client() -> AsyncIterator[ClientSession]:
    async with ClientSession() as session:
        yield session
