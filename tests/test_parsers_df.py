"""DF 检定规划器解析器测试：识别、题量预算、dice_checks 合成。"""

from __future__ import annotations

import json

import pytest

from conftest import build_df_request, make_df_player
from systemone_bridge.parsers import DfCheckPlannerParser, ParseError, extract_request


def _noul(p_yes: float) -> dict:
    return {
        "probabilities": {"no": 1 - p_yes, "yes": p_yes},
        "confidence": max(p_yes, 1 - p_yes),
        "decision": "yes" if p_yes >= 0.5 else "no",
        "noul": p_yes,
        "type": "noul",
    }


def _choice(options: list[str], best: str) -> dict:
    probabilities = {option: (0.8 if option == best else 0.2 / (len(options) - 1)) for option in options}
    return {
        "probabilities": probabilities,
        "confidence": probabilities[best],
        "decision": best,
        "choice": best,
        "type": "choice",
    }


def _score(criteria: dict[str, str], best_key: str) -> dict:
    probabilities = {key: (0.7 if key == best_key else 0.3 / (len(criteria) - 1)) for key in criteria}
    return {
        "probabilities": probabilities,
        "confidence": probabilities[best_key],
        "decision": best_key,
        "score": sum(float(key) * p for key, p in probabilities.items()),
        "legend": dict(criteria),
        "type": "score",
    }


def test_detect_positive_and_negative() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(build_df_request([make_df_player()]))
    assert parser.detect(request) is True

    # 有工具但没有 planner 模板锚点
    body = build_df_request([make_df_player()])
    body["messages"][0]["content"] = "你是 GM 助手。"
    assert parser.detect(extract_request(body)) is False

    # 有模板锚点但没有 dice_checks 工具
    body = build_df_request([make_df_player()])
    body["tools"] = []
    assert parser.detect(extract_request(body)) is False


def test_parse_d20_two_players() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(build_df_request([make_df_player("u1"), make_df_player("u2")]))
    parsed = parser.parse(request)

    assert set(parsed.questions.keys()) == {
        "p0_needs_check", "p0_stat", "p0_kind", "p0_dc",
        "p1_needs_check", "p1_stat", "p1_kind", "p1_dc",
    }
    dc = parsed.questions["p0_dc"]
    assert dc["type"] == "score"
    # score 题必须用数字字符串键，保证期望分即真实 DC
    assert set(dc["criteria"].keys()) == {"10", "15", "20", "25"}
    stat = parsed.questions["p0_stat"]
    assert set(stat["criteria"].keys()) == {
        "strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma",
    }
    # state 携带完整原始上下文
    assert parsed.state["context"]["players"][0]["player_id"] == "u1"


def test_parse_d20_player_budget_overflow() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(build_df_request([make_df_player(f"u{i}") for i in range(5)]))
    with pytest.raises(ParseError, match="题量预算"):
        parser.parse(request)


def test_parse_d100_stat_options_merge_skills_and_attributes() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(
        build_df_request([make_df_player("u1")], dice_system="d100")
    )
    parsed = parser.parse(request)

    assert set(parsed.questions.keys()) == {"p0_needs_check", "p0_stat", "p0_kind"}
    stat_options = set(parsed.questions["p0_stat"]["criteria"].keys())
    # 技能名来自角色卡，属性键来自 ruleset 的 canonical 键（工具 schema 要求 canonical）
    assert stat_options == {"侦查", "潜行",
                            "strength", "dexterity", "constitution",
                            "intelligence", "wisdom", "charisma"}


def test_parse_d100_player_budget() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(
        build_df_request([make_df_player(f"u{i}") for i in range(6)], dice_system="d100")
    )
    with pytest.raises(ParseError):
        parser.parse(request)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["messages"].__setitem__(1, {"role": "user", "content": "不是JSON"}),
        lambda body: body["messages"].__setitem__(1, {"role": "user", "content": '{"players": 1}'}),
        lambda body: body["messages"].__setitem__(
            1, {"role": "user", "content": json.dumps({"players": [], "ruleset": {"dice_system": "none"}})},
        ),
    ],
)
def test_parse_rejects_malformed_context(mutate) -> None:
    parser = DfCheckPlannerParser()
    body = build_df_request([make_df_player()])
    mutate(body)
    with pytest.raises(ParseError):
        parser.parse(extract_request(body))


def test_build_response_mixed_needs_check() -> None:
    parser = DfCheckPlannerParser()
    players = [make_df_player("u1"), make_df_player("u2")]
    request = extract_request(build_df_request(players))
    parsed = parser.parse(request)
    questions = parsed.questions

    answers = {
        "p0_needs_check": _noul(0.9),
        "p0_kind": _choice(["check", "attack", "save"], "check"),
        "p0_stat": _choice(list(questions["p0_stat"]["criteria"].keys()), "strength"),
        "p0_dc": _score(questions["p0_dc"]["criteria"], "20"),
        "p1_needs_check": _noul(0.1),  # 不需要检定 → 不生成条目
        "p1_kind": _choice(["check", "attack", "save"], "attack"),
        "p1_stat": _choice(list(questions["p1_stat"]["criteria"].keys()), "dexterity"),
        "p1_dc": _score(questions["p1_dc"]["criteria"], "15"),
    }
    response = parser.build_response(request, parsed, answers)

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tool_call = choice["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "dice_checks"
    arguments = json.loads(tool_call["function"]["arguments"])
    check = arguments["checks"]
    assert len(check) == 1
    entry = check[0]
    assert entry["player"] == "u1"
    assert entry["kind"] == "check"
    assert entry["attribute"] == "strength"
    # score 题答案为期望分：0.7×20 + 0.1×(10+15+25) = 19.0 → target 19
    assert entry["target"] == 19
    assert "19" in entry["dc_reason"]
    assert entry["advantage"] == "normal"
    assert "听动静" in entry["reason"]


def test_build_response_d100_skill_vs_attribute() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(build_df_request([make_df_player("u1")], dice_system="d100"))
    parsed = parser.parse(request)
    stat_options = list(parsed.questions["p0_stat"]["criteria"].keys())

    answers = {
        "p0_needs_check": _noul(0.95),
        "p0_kind": _choice(["check", "attack", "save"], "save"),
        "p0_stat": _choice(stat_options, "侦查"),  # 命中技能 → skill 字段
    }
    response = parser.build_response(request, parsed, answers)
    entry = json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["checks"][0]
    assert entry["skill"] == "侦查"
    assert "attribute" not in entry
    assert "target" not in entry  # d100 由服务端算阈值

    answers["p0_stat"] = _choice(stat_options, "wisdom")  # 属性 → attribute 字段
    response = parser.build_response(request, parsed, answers)
    entry = json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["checks"][0]
    assert entry["attribute"] == "wisdom"
    assert "skill" not in entry


def test_build_response_dc_clamped_to_max() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(build_df_request([make_df_player("u1")], max_check_dc=25))
    parsed = parser.parse(request)
    answers = {
        "p0_needs_check": _noul(0.9),
        "p0_kind": _choice(["check", "attack", "save"], "check"),
        "p0_stat": _choice(list(parsed.questions["p0_stat"]["criteria"].keys()), "strength"),
        # 极端期望分 99 → 被夹到 max_check_dc=25
        "p0_dc": {"score": 99.0, "legend": {"10": "easy"}, "probabilities": {"10": 1.0}, "decision": "10", "type": "score"},
    }
    response = parser.build_response(request, parsed, answers)
    entry = json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["checks"][0]
    assert entry["target"] == 25


def test_build_response_empty_checks_when_all_negative() -> None:
    parser = DfCheckPlannerParser()
    request = extract_request(build_df_request([make_df_player("u1")]))
    parsed = parser.parse(request)
    answers = {
        "p0_needs_check": _noul(0.2),
        "p0_kind": _choice(["check", "attack", "save"], "check"),
        "p0_stat": _choice(list(parsed.questions["p0_stat"]["criteria"].keys()), "strength"),
        "p0_dc": _score(parsed.questions["p0_dc"]["criteria"], "15"),
    }
    response = parser.build_response(request, parsed, answers)
    arguments = json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert arguments == {"checks": []}
