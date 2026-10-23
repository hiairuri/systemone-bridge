"""exprsel 解析器测试：真实快照 fixture + 阈值/上限回填逻辑。"""

from __future__ import annotations

import re

import pytest

from conftest import exprsel_request, load_snapshot, snapshot_to_messages
from systemone_bridge.parsers import ExpressionSelectorParser, ParseError, extract_request

SNAPSHOTS = [
    "exprsel_group_a.json",
    "exprsel_private_a.json",
    "exprsel_group_b.json",
    "exprsel_group_maiwork_note.json",
]

_CANDIDATE_LINE = re.compile(r"^(\d+):\s*情景=(.*?)\s*\|\s*风格=(.*)$", re.MULTILINE)


def _candidate_ids(system_text: str) -> list[str]:
    return [match.group(1) for match in _CANDIDATE_LINE.finditer(system_text)]


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_detect_real_snapshots(name: str) -> None:
    parser = ExpressionSelectorParser()
    request = extract_request(exprsel_request(name))
    assert parser.detect(request) is True


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_parse_real_snapshots(name: str) -> None:
    parser = ExpressionSelectorParser()
    request = extract_request(exprsel_request(name))
    parsed = parser.parse(request)

    expected_ids = _candidate_ids(request.system_text)
    assert expected_ids, "快照应含候选清单"
    criteria = parsed.questions["selection"]["criteria"]
    assert list(criteria.keys()) == expected_ids  # 顺序保留（协议要求）
    assert len(expected_ids) <= 62  # SystemOne 单题选项上限
    assert parsed.questions["selection"]["type"] == "choice"
    assert parsed.state["recent_chat"] == request.user_texts
    assert all(value.startswith("情景=") for value in criteria.values())


def test_detect_negative() -> None:
    parser = ExpressionSelectorParser()
    body = {
        "model": "intern-decision-4b",
        "messages": [
            {"role": "system", "content": "你是一个普通助手。"},
            {"role": "user", "content": "你好"},
        ],
    }
    assert parser.detect(extract_request(body)) is False


def test_parse_rejects_missing_candidates() -> None:
    parser = ExpressionSelectorParser()
    body = {
        "model": "intern-decision-4b",
        "messages": [
            {"role": "system", "content": "请从候选中选择。候选表达方式：\n（空）"},
            {"role": "user", "content": "上下文"},
        ],
    }
    with pytest.raises(ParseError):
        parser.parse(extract_request(body))


def test_parse_rejects_over_62_candidates() -> None:
    parser = ExpressionSelectorParser()
    lines = "\n".join(
        f"{1000 + index}: 情景=场景{index} | 风格=风格{index}" for index in range(63)
    )
    body = {
        "model": "intern-decision-4b",
        "messages": [
            {"role": "system", "content": f"候选表达方式：\n{lines}"},
            {"role": "user", "content": "上下文"},
        ],
    }
    request = extract_request(body)
    assert parser.detect(request) is True
    with pytest.raises(ParseError):
        parser.parse(request)


def test_build_response_threshold_and_cap() -> None:
    parser = ExpressionSelectorParser(min_probability=0.05, max_selected=5)
    request = extract_request(exprsel_request("exprsel_group_a.json"))
    parsed = parser.parse(request)
    ids = list(parsed.questions["selection"]["criteria"].keys())

    # 前两名高于阈值，第三名低于阈值，其余更低
    probabilities = {expr_id: 0.01 for expr_id in ids}
    probabilities[ids[0]] = 0.5
    probabilities[ids[1]] = 0.3
    probabilities[ids[2]] = 0.04
    response = parser.build_response(request, parsed, {"selection": {"probabilities": probabilities}})

    import json

    choice = response["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    payload = json.loads(choice["message"]["content"])
    assert payload == {"selected_ids": [int(ids[0]), int(ids[1])]}
    assert all(isinstance(value, int) for value in payload["selected_ids"])


def test_build_response_empty_when_all_below_threshold() -> None:
    parser = ExpressionSelectorParser(min_probability=0.05, max_selected=5)
    request = extract_request(exprsel_request("exprsel_group_a.json"))
    parsed = parser.parse(request)
    ids = list(parsed.questions["selection"]["criteria"].keys())
    # 全部低于阈值 → 空选择（对应模板"没有明显合适的就返回空数组"）
    probabilities = {expr_id: 0.02 for expr_id in ids}
    response = parser.build_response(request, parsed, {"selection": {"probabilities": probabilities}})
    import json

    assert json.loads(response["choices"][0]["message"]["content"]) == {"selected_ids": []}


def test_build_response_rejects_missing_answer() -> None:
    parser = ExpressionSelectorParser()
    request = extract_request(exprsel_request("exprsel_group_a.json"))
    parsed = parser.parse(request)
    with pytest.raises(ParseError):
        parser.build_response(request, parsed, {})
