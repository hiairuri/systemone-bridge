"""OpenXLab 托管网关形状测试：decisions_path 可配置 + model 字段按配置注入。

官方开源 serving 兼容性是默认行为：decisions_path 默认 /v1/decisions、model 留空不注入。
OpenXLab 网关差异：POST /v1/systemone、请求体顶层必须带 model 字段（网关 400 校验）。
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import ClientSession, web

from conftest import (
    create_mock_fallback_app,
    exprsel_request,
    make_stack_config,
    spawn,
)
from mock_systemone import create_mock_systemone_app, default_handler
from systemone_bridge.config import UpstreamSystemOneConfig
from systemone_bridge.server import create_app
from systemone_bridge.systemone import SystemOneClient


# ---------- 记录型 mock：同时记录命中路径与请求体 ----------


async def test_default_config_keeps_official_shape() -> None:
    """默认配置（官方开源 serving）：路径 /v1/decisions，请求体不含 model 字段。"""

    received: list[tuple[str, dict[str, Any]]] = []

    async def capture(request: web.Request) -> web.Response:
        received.append((request.path, await request.json()))
        return web.json_response(
            {"model": "intern-decision", "request_id": "0" * 32, "answers": default_handler(await request.json()),
             "usage": {"input_tokens": 1, "output_tokens": 1, "decision_count": 1}}
        )

    app = web.Application()
    app.router.add_post("/v1/decisions", capture)
    url, runner = await spawn(app)
    config = UpstreamSystemOneConfig(base_url=url)  # 全默认：decisions_path=/v1/decisions, model=""
    try:
        async with ClientSession() as session:
            client = SystemOneClient(config, session)
            answers = await client.decide(state="状态", questions={"f": {"type": "choice", "criteria": {"a": "甲", "b": "乙"}}})
    finally:
        await runner.cleanup()

    assert answers["f"]["choice"] == "a"
    (path, body) = received[0]
    assert path == "/v1/decisions"
    assert "model" not in body  # 官方 serving 兼容：不注入 model
    assert body["state"] == "状态"


async def test_openxlab_shape_path_and_model_injection() -> None:
    """OpenXLab 配置：路径 /v1/systemone，请求体顶层注入 model 字段。"""

    received: list[tuple[str, dict[str, Any]]] = []

    async def capture(request: web.Request) -> web.Response:
        received.append((request.path, await request.json()))
        return web.json_response(
            {"model": "intern-decision-4b", "request_id": "0" * 32, "answers": default_handler(await request.json()),
             "usage": {"input_tokens": 1, "output_tokens": 1, "decision_count": 1}}
        )

    app = web.Application()
    app.router.add_post("/v1/systemone", capture)
    url, runner = await spawn(app)
    config = UpstreamSystemOneConfig(base_url=url, decisions_path="/v1/systemone", model="intern-decision-4b")
    try:
        async with ClientSession() as session:
            client = SystemOneClient(config, session)
            answers = await client.decide(state="状态", questions={"f": {"type": "choice", "criteria": {"a": "甲", "b": "乙"}}})
    finally:
        await runner.cleanup()

    assert answers["f"]["choice"] == "a"
    (path, body) = received[0]
    assert path == "/v1/systemone"  # OpenXLab 网关路径
    assert body["model"] == "intern-decision-4b"  # 网关校验必需的顶层 model
    assert body["state"] == "状态"


async def test_openxlab_gateway_end_to_end(tmp_path, http_client: ClientSession) -> None:
    """三件套全链路：exprsel 北向请求 → 桥 → mock /v1/systemone（带 model）→ 合法回包。"""

    received: list[dict[str, Any]] = []

    def capture(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        return default_handler(request)

    systemone_url, runner_so = await spawn(create_mock_systemone_app(capture))
    fallback_calls: list[dict[str, Any]] = []
    fallback_url, runner_fb = await spawn(create_mock_fallback_app(fallback_calls))
    config = make_stack_config(
        systemone_url,
        fallback_url + "/v1",
        upstream_systemone={
            "base_url": systemone_url,
            "decisions_path": "/v1/systemone",
            "api_key": "sk-test-000000",
            "model": "intern-decision-4b",
            "timeout_seconds": 5,
        },
    )
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = exprsel_request("exprsel_group_a.json")
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 200
                data = await response.json()
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
        await runner_fb.cleanup()

    # 南向请求命中网关别名路径，且顶层带 model 字段
    assert len(received) == 1
    assert received[0]["model"] == "intern-decision-4b"
    assert "selection" in received[0]["questions"]
    # 回包是合法 chat completion，内容含决策结果
    assert data["object"] == "chat.completion"
    assert data["model"] == "intern-decision-4b"
    content = json.loads(data["choices"][0]["message"]["content"])
    assert set(content.keys()) == {"selected_ids"}
    assert isinstance(content["selected_ids"], list) and content["selected_ids"]
    # 回退上游未被触碰
    assert fallback_calls == []
