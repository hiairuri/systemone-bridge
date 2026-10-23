"""路由与直通回退测试：模型名路由、各类失败回退、流式透传。"""

from __future__ import annotations

import json

from aiohttp import ClientSession, web

from conftest import build_df_request, create_mock_fallback_app, exprsel_request, make_df_player, make_stack_config, spawn
from mock_systemone import create_mock_systemone_app
from systemone_bridge.server import create_app


async def test_models_endpoint(stack: Stack, http_client: ClientSession) -> None:
    async with http_client.get(f"{stack.bridge_url}/v1/models") as response:
        assert response.status == 200
        data = await response.json()
    assert [item["id"] for item in data["data"]] == [
        "intern-decision-4b", "intern-decision-0.8b",
    ]
    assert data["object"] == "list"


async def test_non_intern_model_direct_passthrough(
    stack: Stack, http_client: ClientSession
) -> None:
    body = {
        "model": "GLM-5.3-Flash",
        "messages": [{"role": "user", "content": "你好"}],
    }
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()
    assert data["choices"][0]["message"]["content"] == "fallback-ok"
    assert len(stack.fallback_calls) == 1
    assert stack.fallback_calls[0]["model"] == "GLM-5.3-Flash"  # 非路由模型保留原名
    assert stack.systemone_received == []


async def test_parse_error_falls_back_and_rewrites_model(
    stack: Stack, http_client: ClientSession
) -> None:
    # exprsel 锚点存在但无候选 → 解析失败 → 回退；模型名 intern-decision-* 改写为回退模型
    body = exprsel_request("exprsel_group_a.json")
    body["messages"][0]["content"] = "请从下面候选中选择。候选表达方式：\n（本次为空）"
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()
    assert data["choices"][0]["message"]["content"] == "fallback-ok"
    assert stack.fallback_calls[0]["model"] == "GLM-TEST"


async def test_systemone_unreachable_falls_back(
    tmp_path, http_client: ClientSession
) -> None:
    calls: list[dict] = []
    fallback_url, runner_fb = await spawn(create_mock_fallback_app(calls))
    # 指向一个必然拒绝连接的端口
    config = make_stack_config("http://127.0.0.1:1", fallback_url + "/v1")
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = exprsel_request("exprsel_group_a.json")
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 200
                data = await response.json()
            assert data["choices"][0]["message"]["content"] == "fallback-ok"
            assert len(calls) == 1
            assert calls[0]["model"] == "GLM-TEST"
            await runner_bridge.cleanup()
    finally:
        await runner_fb.cleanup()


async def test_systemone_500_falls_back(
    tmp_path, http_client: ClientSession
) -> None:
    calls: list[dict] = []

    def boom(_: dict) -> web.Response:
        return web.json_response({"detail": "kaboom"}, status=500)

    systemone_url, runner_so = await spawn(create_mock_systemone_app(boom))
    fallback_url, runner_fb = await spawn(create_mock_fallback_app(calls))
    config = make_stack_config(systemone_url, fallback_url + "/v1")
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = exprsel_request("exprsel_group_a.json")
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 200
                data = await response.json()
            assert data["choices"][0]["message"]["content"] == "fallback-ok"
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
        await runner_fb.cleanup()


async def test_systemone_rejected_422_falls_back(
    tmp_path, http_client: ClientSession
) -> None:
    calls: list[dict] = []

    def reject(_: dict) -> web.Response:
        return web.json_response({"detail": "Supply 1–16 questions."}, status=422)

    systemone_url, runner_so = await spawn(create_mock_systemone_app(reject))
    fallback_url, runner_fb = await spawn(create_mock_fallback_app(calls))
    config = make_stack_config(systemone_url, fallback_url + "/v1")
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = build_df_request([make_df_player("u1")])
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 200
                data = await response.json()
            assert data["choices"][0]["message"]["content"] == "fallback-ok"
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
        await runner_fb.cleanup()


async def test_fallback_upstream_failure_returns_502(
    tmp_path, http_client: ClientSession
) -> None:
    systemone_url, runner_so = await spawn(create_mock_systemone_app())
    fallback_url, runner_fb = await spawn(create_mock_fallback_app([], status=500))
    config = make_stack_config(systemone_url, fallback_url + "/v1")
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = {"model": "GLM-5.3-Flash", "messages": [{"role": "user", "content": "hi"}]}
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 502
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
        await runner_fb.cleanup()


async def test_stream_passthrough_relays_sse(
    tmp_path, http_client: ClientSession
) -> None:
    calls: list[dict] = []
    sse = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hel"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    systemone_url, runner_so = await spawn(create_mock_systemone_app())
    fallback_url, runner_fb = await spawn(
        create_mock_fallback_app(calls, sse_chunks=sse)
    )
    config = make_stack_config(systemone_url, fallback_url + "/v1")
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = {
                "model": "GLM-5.3-Flash",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 200
                assert response.content_type == "text/event-stream"
                raw = await response.read()
            assert raw == b"".join(sse)
            assert calls[0]["stream"] is True
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
        await runner_fb.cleanup()


async def test_translation_path_stream_true_sends_sse(
    stack: Stack, http_client: ClientSession
) -> None:
    body = exprsel_request("exprsel_group_a.json", stream=True)
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        assert response.content_type == "text/event-stream"
        raw = await response.read()
    lines = [line for line in raw.decode("utf-8").split("\n") if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    first = json.loads(lines[0][len("data: "):])
    assert first["object"] == "chat.completion.chunk"
    delta = first["choices"][0]["delta"]
    assert delta["role"] == "assistant"
    assert "selected_ids" in delta["content"]
    last = json.loads(lines[-2][len("data: "):])
    assert last["choices"][0]["finish_reason"] == "stop"


async def test_invalid_body_400(stack: Stack, http_client: ClientSession) -> None:
    async with http_client.post(
        f"{stack.bridge_url}/v1/chat/completions", data="not json at all"
    ) as response:
        assert response.status == 400
