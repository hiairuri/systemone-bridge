"""错误码测试：纯协议转换器的全部失败路径（422×2 / 502 / 500 / 400）与错误体形状。"""

from __future__ import annotations

from typing import Any

from aiohttp import ClientSession, web

from conftest import make_stack_config, exprsel_request, spawn
from mock_systemone import create_mock_systemone_app
from systemone_bridge.config import BridgeConfig
from systemone_bridge.server import create_app


def assert_openai_error(data: dict[str, Any], code: str) -> dict[str, Any]:
    """错误体统一 OpenAI 风格：{"error": {"message", "type", "code"}}。"""

    error = data["error"]
    assert set(error.keys()) == {"message", "type", "code"}
    assert error["code"] == code
    assert error["message"]
    assert error["type"]
    return error


async def test_models_endpoint(stack, http_client: ClientSession) -> None:
    async with http_client.get(f"{stack.bridge_url}/v1/models") as response:
        assert response.status == 200
        data = await response.json()
    assert [item["id"] for item in data["data"]] == [
        "intern-decision-4b", "intern-decision-0.8b",
    ]
    assert data["object"] == "list"


async def test_no_anchor_returns_422_unknown_template(
    stack, http_client: ClientSession
) -> None:
    body = {
        "model": "intern-decision-4b",
        "messages": [{"role": "user", "content": "你好"}],
    }
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 422
        data = await response.json()
    assert_openai_error(data, "unknown_template")
    assert stack.systemone_received == []  # 无锚点不触发南向调用


async def test_model_name_no_longer_routes(stack, http_client: ClientSession) -> None:
    """模型名不再参与路由：任意模型名一律尝试解析器（无锚点同样 422）。"""

    body = {
        "model": "GLM-5.3-Flash",
        "messages": [{"role": "user", "content": "你好"}],
    }
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 422
        data = await response.json()
    assert_openai_error(data, "unknown_template")


async def test_anchor_without_candidates_422_bad_template(
    stack, http_client: ClientSession
) -> None:
    body = exprsel_request("exprsel_group_a.json")
    body["messages"][0]["content"] = "请从下面候选中选择。候选表达方式：\n（本次为空）"
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 422
        data = await response.json()
    assert_openai_error(data, "bad_template")
    assert "expression_selector" in data["error"]["message"]


async def test_systemone_unreachable_502_upstream_error(
    tmp_path, http_client: ClientSession
) -> None:
    config = make_stack_config("http://127.0.0.1:1")  # 必然拒绝连接
    async with create_app(config, tmp_path) as bridge_app:
        bridge_url, runner_bridge = await spawn(bridge_app)
        body = exprsel_request("exprsel_group_a.json")
        async with http_client.post(
            f"{bridge_url}/v1/chat/completions", json=body
        ) as response:
            assert response.status == 502
            data = await response.json()
        await runner_bridge.cleanup()
    error = assert_openai_error(data, "upstream_error")
    # 原四分类并入错误 message（unreachable 保留在细节里）
    assert "unreachable" in error["message"]


async def test_systemone_500_maps_to_502(tmp_path, http_client: ClientSession) -> None:
    def boom(_: dict) -> web.Response:
        return web.json_response({"detail": "kaboom"}, status=500)

    systemone_url, runner_so = await spawn(create_mock_systemone_app(boom))
    config = make_stack_config(systemone_url)
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = exprsel_request("exprsel_group_a.json")
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 502
                data = await response.json()
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
    error = assert_openai_error(data, "upstream_error")
    assert "server_error" in error["message"]  # 原分类保留在 message
    assert "kaboom" in error["message"]  # 上游细节透出


async def test_systemone_4xx_maps_to_502(tmp_path, http_client: ClientSession) -> None:
    def reject(_: dict) -> web.Response:
        return web.json_response({"detail": "Supply 1–16 questions."}, status=422)

    systemone_url, runner_so = await spawn(create_mock_systemone_app(reject))
    config = make_stack_config(systemone_url)
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = exprsel_request("exprsel_group_a.json")
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 502
                data = await response.json()
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
    error = assert_openai_error(data, "upstream_error")
    assert "rejected" in error["message"]
    assert "questions." in error["message"]  # 上游 body 细节透出（JSON 转义形式）


async def test_stream_true_400_streaming_not_supported(
    stack, http_client: ClientSession
) -> None:
    body = exprsel_request("exprsel_group_a.json", stream=True)
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 400
        data = await response.json()
    assert_openai_error(data, "streaming_not_supported")


async def test_build_error_500(tmp_path, http_client: ClientSession) -> None:
    """mock 返回的 answers 缺少 selection 题 → 响应合成失败 → 500 build_error。"""

    def wrong_question(_: dict) -> dict[str, Any]:
        return {
            "unrelated": {
                "type": "choice",
                "probabilities": {"a": 0.9, "b": 0.1},
                "confidence": 0.9,
                "decision": "a",
                "choice": "a",
                "source": "local",
            }
        }

    systemone_url, runner_so = await spawn(create_mock_systemone_app(wrong_question))
    config = make_stack_config(systemone_url)
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = exprsel_request("exprsel_group_a.json")
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 500
                data = await response.json()
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
    assert_openai_error(data, "build_error")


async def test_invalid_json_400(stack, http_client: ClientSession) -> None:
    async with http_client.post(
        f"{stack.bridge_url}/v1/chat/completions", data="not json at all"
    ) as response:
        assert response.status == 400
        data = await response.json()
    assert_openai_error(data, "invalid_request")


async def test_missing_messages_400(stack, http_client: ClientSession) -> None:
    async with http_client.post(
        f"{stack.bridge_url}/v1/chat/completions", json={"model": "intern-decision-4b"}
    ) as response:
        assert response.status == 400
        data = await response.json()
    assert_openai_error(data, "invalid_request")


async def test_bridge_config_without_fallback_section() -> None:
    """配置模型不再有 fallback 节：旧配置里的 fallback 段不会污染新模型。"""

    config = BridgeConfig()
    assert "fallback" not in config.model_dump()
