"""端到端测试：北向进 → 翻译 → mock SystemOne → 合法 OpenAI 回包。"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import ClientSession

from conftest import Stack, build_df_request, exprsel_request, make_df_player
from mock_systemone import validate_like_official


def _first_candidate_id(system_text: str) -> int:
    import re

    match = re.search(r"^(\d+):\s*情景=", system_text, re.MULTILINE)
    assert match is not None
    return int(match.group(1))


def _count_candidates(system_text: str) -> int:
    import re

    return len(re.findall(r"^\d+:\s*情景=.*\|\s*风格=", system_text, re.MULTILINE))


async def test_exprsel_end_to_end(stack: Stack, http_client: ClientSession) -> None:
    """真实快照进桥 → mock SystemOne → 回包格式合法且选中首候选。"""

    name = "exprsel_group_a.json"
    body = exprsel_request(name)
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()

    # OpenAI 回包形状
    assert data["object"] == "chat.completion"
    assert data["model"] == "intern-decision-4b"
    assert data["id"].startswith("chatcmpl-")
    choice = data["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    content = json.loads(choice["message"]["content"])
    assert set(content.keys()) == {"selected_ids"}

    # mock SystemOne 首选项 0.9 → 阈值 0.05 → 只入选首候选
    system_text = body["messages"][0]["content"]
    assert content["selected_ids"] == [_first_candidate_id(system_text)]

    # 南向请求符合官方校验约束
    assert len(stack.systemone_received) == 1
    assert stack.systemone_received[0]["questions"]["selection"]["type"] == "choice"
    assert validate_like_official(stack.systemone_received[0]) is None
    assert _count_candidates(system_text) == len(
        stack.systemone_received[0]["questions"]["selection"]["criteria"]
    )


async def test_df_end_to_end(stack: Stack, http_client: ClientSession) -> None:
    """DF 检定请求进桥 → mock SystemOne → dice_checks tool_calls。"""

    body = build_df_request([make_df_player("u1"), make_df_player("u2")])
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()

    assert data["model"] == "intern-decision-4b"
    message = data["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = message["tool_calls"][0]
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "dice_checks"
    arguments = json.loads(tool_call["function"]["arguments"])
    # mock 默认 noul yes=0.9 → 两位玩家都出检定；d20 默认属性首选项/DC 首档
    assert len(arguments["checks"]) == 2
    first = arguments["checks"][0]
    assert first["player"] == "u1"
    assert first["attribute"] in {"strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"}
    assert isinstance(first["target"], int)
    assert 1 <= first["target"] <= 100

    # 南向请求符合官方校验（2 玩家 × 4 题 = 8 题 ≤ 16）
    assert len(stack.systemone_received) == 1
    assert len(stack.systemone_received[0]["questions"]) == 8
    assert validate_like_official(stack.systemone_received[0]) is None


async def test_df_partial_needs_check_end_to_end(
    stack: Stack, http_client: ClientSession, tmp_path
) -> None:
    """p0 要检定、p1 不要：单个 mock handler 里按字段名分流。"""

    from mock_systemone import _choice_answer, _noul_answer, create_mock_systemone_app, spawn
    from conftest import make_stack_config
    from systemone_bridge.server import create_app

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for field_name, question in request["questions"].items():
            if field_name.endswith("_needs_check"):
                answers[field_name] = _noul_answer(0.9 if field_name.startswith("p0") else 0.1)
            elif field_name.endswith("_dc"):
                options = list(question["criteria"])
                answers[field_name] = _choice_answer("score", options, options[1])
            elif field_name.endswith("_stat"):
                options = list(question["criteria"])
                answers[field_name] = _choice_answer("choice", options, options[0])
            else:
                options = list(question["criteria"])
                answers[field_name] = _choice_answer("choice", options, options[0])
        return answers

    # 为本用例换一个带 handler 的 SystemOne：重建双件套
    received: list[dict] = []

    def capture(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        return handler(request)

    systemone_url, runner_so = await spawn(create_mock_systemone_app(capture))
    config = make_stack_config(systemone_url)
    try:
        async with create_app(config, tmp_path) as bridge_app:
            bridge_url, runner_bridge = await spawn(bridge_app)
            body = build_df_request([make_df_player("u1"), make_df_player("u2")])
            async with http_client.post(
                f"{bridge_url}/v1/chat/completions", json=body
            ) as response:
                assert response.status == 200
                data = await response.json()
            arguments = json.loads(
                data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            )
            assert [check["player"] for check in arguments["checks"]] == ["u1"]
            await runner_bridge.cleanup()
    finally:
        await runner_so.cleanup()
