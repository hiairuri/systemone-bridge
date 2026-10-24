"""三决策解析器测试：expression_check / jargon_compare / emoji_replace。

命中 fixture 由真实模板（prompts/zh-CN/*.prompt）程序化渲染生成，字段值取自
MaiBot 真实证据（review_logs.json、jargon_learning_update 快照、emoji_manager 拼装逻辑）。
负例逐字引用相邻任务模板（learn_style / jargon_inference_with_context / emoji_selection）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import load_snapshot
from systemone_bridge.parsers import (
    EmojiReplaceParser,
    ExpressionCheckParser,
    JargonCompareParser,
    ParseError,
    extract_request,
)

MODEL = "systemone-exprsel"


def _request_from_fixture(name: str) -> dict[str, Any]:
    return load_snapshot(name)


def _request_from_text(text: str, role: str = "user") -> dict[str, Any]:
    return {"model": MODEL, "messages": [{"role": role, "content": text}]}


# ---------- 相邻任务模板（逐字引用，渲染后形状） ----------

# learn_style.prompt（expression_learner 生成型任务）：含「表达方式」字样但无审核锚点
LEARN_STYLE_RENDERED = (
    "对某件事表示十分惊叹 [source_id:3]\n"
    "请从上面这段群聊中提取用户的语言风格和说话方式。\n"
    "请总结成如下格式的规律：当 \"AAAAA\" 时，可以 \"BBBBB\"。\n"
    "请仅输出 JSON 数组，不要输出重复内容。\n"
    "输出 JSON："
)
# jargon_inference_with_context.prompt（jargon compare 的前置相，生成型）
JARGON_INFERENCE_RENDERED = (
    "**词条内容**\n钓饵\n"
    "**词条出现的上下文。其中的小狐狸的发言内容是你自己的发言**\n"
    "<message>这个 oferta 就是钓饵</message>\n"
    "请根据上下文，推断\"钓饵\"这个词条的含义。\n"
    "以 JSON 格式输出：\n"
    "{\n  \"meaning\": \"详细含义说明（包含使用场景、来源、具体解释等）\",\n  \"no_info\": false\n}"
)
# emoji_selection.prompt（send_emoji 选择子代理，带图 choice）
EMOJI_SELECTION_RENDERED = (
    "你需要根据上下文和当前语气,选择一个合适的表情包来发送\n"
    "其中包含一张 5x5 的表情包拼图，一共 25 个位置。\n"
    "每张小图左上角都有一个较大的序号，范围是 1 到 25。\n"
    "你需要从这 25 张图里选出最合适的一张表情包。\n"
    "你必须返回一个 JSON 对象（json object），不要输出任何 JSON 之外的内容。\n"
    "返回格式为：{\"emoji_index\":1,\"reason\":\"简短理由\"}"
)


# ---------- expression_check ----------


def test_expression_check_detect_hit() -> None:
    parser = ExpressionCheckParser()
    request = extract_request(_request_from_fixture("expression_check_hit.json"))
    assert parser.detect(request) is True


def test_expression_check_parse_hit() -> None:
    parser = ExpressionCheckParser()
    request = extract_request(_request_from_fixture("expression_check_hit.json"))
    parsed = parser.parse(request)

    question = parsed.questions["suitability"]
    assert question["type"] == "noul"
    assert question["instructions"].strip()  # OpenXLab 网关要求 instructions 必带
    # OpenXLab 网关要求 noul 带且仅带 true/false 键的 criteria（官方 serving 亦兼容）
    assert set(question["criteria"].keys()) == {"true", "false"}
    # state 结构化对象：situation/style 取自模板字段行，criteria 为调用方固定 4 条
    assert parsed.state["situation"] == "描写自己身体或外貌"
    assert parsed.state["style"] == "用「白白嫩嫩」叠词卖萌"
    assert len(parsed.state["criteria"]) == 4
    assert isinstance(parsed.state, dict)


def test_expression_check_build_response_mapping() -> None:
    parser = ExpressionCheckParser()
    request = extract_request(_request_from_fixture("expression_check_hit.json"))
    parsed = parser.parse(request)

    high = parser.build_response(request, parsed, {"suitability": {"probabilities": {"yes": 0.9, "no": 0.1}}})
    payload = json.loads(high["choices"][0]["message"]["content"])
    assert payload["suitable"] is True
    assert isinstance(payload["reason"], str) and payload["reason"]

    # 阈值边界：P(yes)=0.5 判适合
    edge = json.loads(
        parser.build_response(
            request, parsed, {"suitability": {"probabilities": {"yes": 0.5, "no": 0.5}}}
        )["choices"][0]["message"]["content"]
    )
    assert edge["suitable"] is True

    low = json.loads(
        parser.build_response(
            request, parsed, {"suitability": {"probabilities": {"yes": 0.3, "no": 0.7}}}
        )["choices"][0]["message"]["content"]
    )
    assert low["suitable"] is False

    with pytest.raises(ParseError):
        parser.build_response(request, parsed, {})


def test_expression_check_parse_rejects_missing_fields() -> None:
    parser = ExpressionCheckParser()
    # 锚点命中但字段行缺失 → bad_template 422 路径
    request = extract_request(_request_from_text("请评估以下表达方式或语言风格是否合适：（正文缺失）"))
    assert parser.detect(request) is True
    with pytest.raises(ParseError):
        parser.parse(request)


@pytest.mark.parametrize(
    "text", [LEARN_STYLE_RENDERED, JARGON_INFERENCE_RENDERED, EMOJI_SELECTION_RENDERED]
)
def test_expression_check_detect_negative(text: str) -> None:
    assert ExpressionCheckParser().detect(extract_request(_request_from_text(text))) is False


# ---------- jargon_compare ----------


def test_jargon_compare_detect_hit() -> None:
    parser = JargonCompareParser()
    request = extract_request(_request_from_fixture("jargon_compare_hit.json"))
    assert parser.detect(request) is True


def test_jargon_compare_parse_hit() -> None:
    parser = JargonCompareParser()
    request = extract_request(_request_from_fixture("jargon_compare_hit.json"))
    parsed = parser.parse(request)

    question = parsed.questions["similarity"]
    assert question["type"] == "noul"
    assert question["instructions"].strip()
    assert set(question["criteria"].keys()) == {"true", "false"}
    # 两段内嵌 JSON 解析为对象进 state（结构化）
    inference1, inference2 = parsed.state["inference1"], parsed.state["inference2"]
    assert isinstance(inference1, dict) and "钓饵" in inference1["meaning"]
    assert isinstance(inference2, dict) and inference2["meaning"]
    assert "no_info" in inference1


def test_jargon_compare_parse_keeps_unparseable_inference() -> None:
    parser = JargonCompareParser()
    text = (
        "**推断结果1（基于上下文）**\n（不是 JSON 的推断）\n\n"
        "**推断结果2（仅基于词条）**\n{\"meaning\": \"常规词汇\"}\n\n"
        "请比较这两个推断结果，判断它们是否相同或类似。\n"
    )
    request = extract_request(_request_from_text(text))
    assert parser.detect(request) is True
    parsed = parser.parse(request)
    assert parsed.state["inference1"] == "（不是 JSON 的推断）"
    assert parsed.state["inference2"] == {"meaning": "常规词汇"}


def test_jargon_compare_build_response_mapping() -> None:
    parser = JargonCompareParser()
    request = extract_request(_request_from_fixture("jargon_compare_hit.json"))
    parsed = parser.parse(request)

    similar = json.loads(
        parser.build_response(
            request, parsed, {"similarity": {"probabilities": {"yes": 0.85, "no": 0.15}}}
        )["choices"][0]["message"]["content"]
    )
    assert similar["is_similar"] is True
    assert similar["reason"]

    # 调用方语义：is_similar=false → 是黑话（jargon_miner.py:531-532）
    different = json.loads(
        parser.build_response(
            request, parsed, {"similarity": {"probabilities": {"yes": 0.2, "no": 0.8}}}
        )["choices"][0]["message"]["content"]
    )
    assert different["is_similar"] is False

    with pytest.raises(ParseError):
        parser.build_response(request, parsed, {})


def test_jargon_compare_parse_rejects_missing_sections() -> None:
    parser = JargonCompareParser()
    request = extract_request(_request_from_text("请比较这两个推断结果，判断它们是否相同或类似。（缺标题段）"))
    assert parser.detect(request) is True
    with pytest.raises(ParseError):
        parser.parse(request)


@pytest.mark.parametrize(
    "text",
    [
        JARGON_INFERENCE_RENDERED,  # 前置相：生成型，无比较锚点
        json.dumps({"meaning": "常规含义"}, ensure_ascii=False),
    ],
)
def test_jargon_compare_detect_negative(text: str) -> None:
    assert JargonCompareParser().detect(extract_request(_request_from_text(text))) is False


# ---------- emoji_replace ----------


def test_emoji_replace_detect_hit() -> None:
    parser = EmojiReplaceParser()
    request = extract_request(_request_from_fixture("emoji_replace_hit.json"))
    assert parser.detect(request) is True


def test_emoji_replace_parse_hit() -> None:
    parser = EmojiReplaceParser()
    request = extract_request(_request_from_fixture("emoji_replace_hit.json"))
    parsed = parser.parse(request)

    question = parsed.questions["replacement"]
    assert question["type"] == "choice"
    assert question["instructions"].strip()
    criteria = question["criteria"]
    # 3 个候选编号 + keep；候选描述/使用次数进入选项文案
    assert set(criteria.keys()) == {"1", "2", "3", "keep"}
    assert "白色小猫" in criteria["1"]
    assert criteria["keep"] == "不取消注册：保留全部现有表情包，放弃新表情包"
    assert parsed.state["new_emoji_description"] == "一只戴着墨镜的柴犬摇头晃脑"
    assert [row["number"] for row in parsed.state["candidates"]] == [1, 2, 3]
    assert parsed.state["candidates"][1]["query_count"] == 15


def test_emoji_replace_build_response_unregister_and_keep() -> None:
    parser = EmojiReplaceParser()
    request = extract_request(_request_from_fixture("emoji_replace_hit.json"))
    parsed = parser.parse(request)

    # 无 decision 字段时按概率 argmax
    unregister = parser.build_response(
        request, parsed, {"replacement": {"probabilities": {"1": 0.1, "2": 0.7, "3": 0.1, "keep": 0.1}}}
    )
    assert unregister["choices"][0]["message"]["content"] == "取消注册编号2"

    keep = parser.build_response(
        request, parsed, {"replacement": {"probabilities": {"1": 0.1, "2": 0.1, "3": 0.1, "keep": 0.7}}}
    )
    assert keep["choices"][0]["message"]["content"] == "不取消注册"

    # 端点给出合法 decision 时直接采信
    decided = parser.build_response(
        request, parsed, {"replacement": {"decision": "3", "probabilities": {"1": 0.9}}}
    )
    assert decided["choices"][0]["message"]["content"] == "取消注册编号3"

    # decision 非法时回退 argmax
    fallback = parser.build_response(
        request, parsed, {"replacement": {"decision": "99", "probabilities": {"1": 0.4, "2": 0.2, "3": 0.2, "keep": 0.2}}}
    )
    assert fallback["choices"][0]["message"]["content"] == "取消注册编号1"

    with pytest.raises(ParseError):
        parser.build_response(request, parsed, {})


def test_emoji_replace_parse_rejects_empty_list() -> None:
    parser = EmojiReplaceParser()
    request = extract_request(_request_from_text(
        "小狐狸的可发送表情包数量已满(20/20)，需要决定是否取消注册一个旧表情包来为新表情包腾出可发送名额。\n"
        "现有表情包列表：\n（空）\n"
    ))
    assert parser.detect(request) is True
    with pytest.raises(ParseError):
        parser.parse(request)


@pytest.mark.parametrize("text", [EMOJI_SELECTION_RENDERED, LEARN_STYLE_RENDERED])
def test_emoji_replace_detect_negative(text: str) -> None:
    assert EmojiReplaceParser().detect(extract_request(_request_from_text(text))) is False


# ---------- 跨解析器互不误识别 ----------


@pytest.mark.parametrize(
    "fixture_name", ["expression_check_hit.json", "jargon_compare_hit.json", "emoji_replace_hit.json"]
)
def test_hit_fixtures_route_to_exactly_one_new_parser(fixture_name: str) -> None:
    """每份命中 fixture 只被目标解析器识别，其余解析器（含 exprsel/DF）全部拒绝。"""

    from systemone_bridge.parsers import DfCheckPlannerParser, ExpressionSelectorParser

    parsers: dict[str, Any] = {
        "expression_check": ExpressionCheckParser(),
        "jargon_compare": JargonCompareParser(),
        "emoji_replace": EmojiReplaceParser(),
        "expression_selector": ExpressionSelectorParser(),
        "df_check_planner": DfCheckPlannerParser(),
    }
    request = extract_request(_request_from_fixture(fixture_name))
    matched = [name for name, parser in parsers.items() if parser.detect(request)]
    expected = fixture_name.removesuffix("_hit.json")
    assert matched == [expected]


# ---------- 路由级端到端：三任务过桥 → mock SystemOne → 调用方期望形状 ----------


async def test_expression_check_end_to_end(stack, http_client) -> None:
    body = _request_from_fixture("expression_check_hit.json")
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()

    content = json.loads(data["choices"][0]["message"]["content"])
    # mock noul yes=0.9 → suitable=true；调用方 parse_evaluation_response 消费 suitable/reason
    assert content["suitable"] is True
    assert "reason" in content

    assert len(stack.systemone_received) == 1
    southbound = stack.systemone_received[0]
    assert southbound["questions"]["suitability"]["type"] == "noul"
    assert isinstance(southbound["state"], dict)  # OpenXLab：state 结构化对象


async def test_jargon_compare_end_to_end(stack, http_client) -> None:
    body = _request_from_fixture("jargon_compare_hit.json")
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()

    content = json.loads(data["choices"][0]["message"]["content"])
    # mock noul yes=0.9 → is_similar=true（非黑话）
    assert content["is_similar"] is True
    assert "reason" in content


async def test_emoji_replace_end_to_end(stack, http_client) -> None:
    body = _request_from_fixture("emoji_replace_hit.json")
    async with http_client.post(f"{stack.bridge_url}/v1/chat/completions", json=body) as response:
        assert response.status == 200
        data = await response.json()

    # mock choice 首选项 0.9 → 编号 1；调用方按自由文本正则解析（emoji_manager.py:916-922）
    assert data["choices"][0]["message"]["content"] == "取消注册编号1"
