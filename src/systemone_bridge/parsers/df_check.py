"""DiceFrame 回合检定规划器（dice_checks 工具调用）解析器。

北向形状（来自 DiceFrame src/commands/check_planner.py 实测）：
- system 消息 = prompts/check_planner_<lang>.md 模板全文（首行「# DiceFrame 回合检定规划器」）；
- user 消息 = `_planner_context()` 产出的**紧凑 JSON 字符串**（round/scene/recent_narration/
  ruleset/players[]/companions/recent_purchases[?]/world_state[?]）；
- tools = [dice_checks]，期望回 OpenAI tool_calls，arguments.checkes 数组，单项必填 player。

映射（本项目设计决策，题量预算 ≤16）：
- 每位玩家 3 题：needs_check(noul)、stat(choice：d100=技能+属性，d20=属性)、kind(choice)；
- d20 额外 1 题：dc(score，criteria 用 {DC值: 档位名} 保证期望分即真实 DC)；
- d20 每玩家 4 题 → 最多 4 人；d100 每玩家 3 题 → 最多 5 人；超限 ParseError → 422 bad_template。
- advantage/modifier/economy_actions/overreach/world_requirements v1 不决策：
  前两者按模板默认（normal/0），后三者 DF 侧独立容错解析、缺失不影响 checks。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from .base import NorthboundRequest, ParsedTask, ParseError, dump_json, tool_names

_TOOL_NAME = "dice_checks"
_SYSTEM_ANCHOR = "回合检定规划器"

# SystemOne 硬限制：单请求 1–16 题、单题 ≤62 选项
_MAX_QUESTIONS = 16
_MAX_OPTIONS = 62

_KINDS = ("check", "attack", "save")


class DfCheckPlannerParser:
    name = "df_check_planner"

    def detect(self, request: NorthboundRequest) -> bool:
        return _TOOL_NAME in tool_names(request) and _SYSTEM_ANCHOR in request.system_text

    def parse(self, request: NorthboundRequest) -> ParsedTask:
        try:
            payload = json.loads(request.user_text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"user 消息不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ParseError("planner 上下文不是 JSON 对象")
        players = payload.get("players")
        ruleset = payload.get("ruleset")
        if not isinstance(players, list) or not isinstance(ruleset, dict):
            raise ParseError("planner 上下文缺少 players/ruleset")
        dice_system = str(ruleset.get("dice_system") or "").lower()
        if dice_system not in {"d20", "d100"}:
            raise ParseError(f"不支持的 dice_system={dice_system!r}")

        attributes = _attribute_keys(ruleset)
        if dice_system == "d20" and not attributes:
            raise ParseError("d20 规则集没有可用属性")
        questions_per_player = 4 if dice_system == "d20" else 3
        max_players = _MAX_QUESTIONS // questions_per_player
        if len(players) > max_players:
            raise ParseError(
                f"玩家数 {len(players)} 超出题量预算（每玩家 {questions_per_player} 题，"
                f"最多 {max_players} 人）"
            )

        questions: dict[str, dict[str, Any]] = {}
        cleaned_players: list[dict[str, Any]] = []
        dc_table = ruleset.get("dc_table") if isinstance(ruleset.get("dc_table"), dict) else {}
        for index, player in enumerate(players):
            parsed_player = _clean_player(player, index, dice_system, ruleset, attributes, dc_table)
            questions.update(parsed_player.pop("questions"))
            cleaned_players.append(parsed_player)
        if not cleaned_players:
            raise ParseError("没有可决策的玩家行动")

        state = {
            "task": (
                "TRPG 回合检定规划：逐玩家判断其本轮行动是否需要系统检定；"
                "需要时给出检定属性/技能、类型"
                + ("与任务难度 DC" if dice_system == "d20" else "")
                + "。只有成功与失败都可能且失败有实质后果时才需要检定。"
            ),
            "dice_system": dice_system,
            "context": payload,
        }
        return ParsedTask(
            kind=self.name, state=state, questions=questions,
            notes=[f"玩家 {len(cleaned_players)} 名，dice_system={dice_system}"],
        )

    def build_response(
        self, request: NorthboundRequest, parsed: ParsedTask, answers: dict[str, Any]
    ) -> dict[str, Any]:
        context = parsed.state.get("context") or {}
        players = context.get("players") or []
        dice_system = str(parsed.state.get("dice_system") or "d20")
        ruleset = context.get("ruleset") or {}
        dc_table = ruleset.get("dc_table") if isinstance(ruleset.get("dc_table"), dict) else {}
        max_dc = ruleset.get("max_check_dc")

        attribute_keys = set(_attribute_keys(ruleset))
        checks: list[dict[str, Any]] = []
        for index, player in enumerate(players):
            if not isinstance(player, dict):
                continue
            needs = answers.get(f"p{index}_needs_check")
            if not isinstance(needs, dict):
                continue
            probabilities = needs.get("probabilities")
            if not isinstance(probabilities, dict):
                continue
            if float(probabilities.get("yes") or 0.0) < 0.5:
                continue
            check = self._build_check(
                index, player, answers, dice_system, dc_table, max_dc, attribute_keys
            )
            if check is not None:
                checks.append(check)

        arguments = dump_json({"checks": checks})
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + secrets.token_hex(12),
                    "type": "function",
                    "function": {"name": _TOOL_NAME, "arguments": arguments},
                }
            ],
        }
        return {
            "id": "chatcmpl-" + secrets.token_hex(12),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls"}
            ],
        }

    def _build_check(
        self,
        index: int,
        player: dict[str, Any],
        answers: dict[str, Any],
        dice_system: str,
        dc_table: dict[str, Any],
        max_dc: Any,
        attribute_keys: set[str],
    ) -> dict[str, Any] | None:
        """把单玩家的 SystemOne 答案合成为合法 dice_checks 条目；不合法返回 None。"""

        kind_answer = answers.get(f"p{index}_kind") or {}
        kind = str(kind_answer.get("decision") or "check")
        if kind not in _KINDS:
            kind = "check"
        stat_answer = answers.get(f"p{index}_stat") or {}
        stat = str(stat_answer.get("decision") or "")

        check: dict[str, Any] = {
            "player": str(player.get("player_id") or ""),
            "kind": kind,
            "advantage": "normal",
        }
        if not check["player"]:
            return None
        skills = {
            str(row.get("name") or "").strip()
            for row in player.get("skills") or []
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        }
        if dice_system == "d100":
            if stat in skills:
                check["skill"] = stat
            elif stat in attribute_keys:
                check["attribute"] = stat
            else:
                return None
        else:
            if stat not in attribute_keys:
                return None
            check["attribute"] = stat
            dc_answer = answers.get(f"p{index}_dc") or {}
            target, confidence = _argmax_dc(dc_answer)
            if target is None:
                return None
            target = min(max(target, 1), 100)
            if isinstance(max_dc, int) and max_dc > 0:
                target = min(target, max_dc)
            check["target"] = target
            check["dc_reason"] = _dc_reason(dc_answer, target, confidence, dc_table)
        action = str(player.get("action") or "")
        if action:
            check["reason"] = f"行动：{action[:140]}"
        return check


def _argmax_dc(dc_answer: dict[str, Any]) -> tuple[int | None, float]:
    """从 score 答案还原 argmax 档位的 DC 值，兼容两种上游形状。

    - OpenXLab 网关：probabilities 按索引（"0","1",…）+ legend 映射档位值；
    - 官方 serving：probabilities 键即档位值（数字字符串）。
    返回 (dc, confidence)；形状不合法返回 (None, 0.0)。
    """

    probabilities = dc_answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        return None, 0.0
    legend = dc_answer.get("legend") if isinstance(dc_answer.get("legend"), dict) else {}
    best_key: str | None = None
    best_prob = -1.0
    for key, prob in probabilities.items():
        try:
            value = float(prob)
        except (TypeError, ValueError):
            continue
        if value > best_prob:
            best_prob = value
            best_key = str(key)
    if best_key is None:
        return None, 0.0
    # 候选依次尝试：legend 映射值（网关形状）→ 概率键本身（值键形状）。
    # legend 值可能是非整数（描述文本或概率副本），失败即回落，两种上游形状都兼容。
    candidates = []
    if legend:
        candidates.append(legend.get(best_key))
    candidates.append(best_key)
    for candidate in candidates:
        try:
            return int(str(candidate)), max(best_prob, 0.0)
        except (TypeError, ValueError):
            continue
    return None, 0.0


def _dc_reason(
    dc_answer: dict[str, Any], target: int, confidence: float, dc_table: dict[str, Any]
) -> str:
    """给出可审计的 DC 依据：argmax 档位 + 置信度。"""

    label = ""
    for key, value in dc_table.items():
        try:
            if int(value) == target:
                label = str(key)
                break
        except (TypeError, ValueError):
            continue
    suffix = f"（档位：{label}）" if label else ""
    return f"Decision argmax 档位 DC{target}，置信度 {confidence:.2f}{suffix}"[:160]


def _attribute_keys(ruleset: dict[str, Any]) -> list[str]:
    attributes = ruleset.get("attributes")
    keys: list[str] = []
    if isinstance(attributes, list):
        for item in attributes:
            if isinstance(item, dict):
                key = str(item.get("key") or "").strip()
                name = str(item.get("name") or "").strip()
                if key:
                    keys.append(key)
                elif name:
                    keys.append(name)
            elif str(item).strip():
                keys.append(str(item).strip())
    return keys


def _clean_player(
    player: Any,
    index: int,
    dice_system: str,
    ruleset: dict[str, Any],
    attributes: list[str],
    dc_table: dict[str, Any],
) -> dict[str, Any]:
    """校验并规整单玩家上下文，同时产出该玩家的 SystemOne 题目。"""

    if not isinstance(player, dict):
        raise ParseError(f"players[{index}] 不是对象")
    player_id = str(player.get("player_id") or "").strip()
    if not player_id:
        raise ParseError(f"players[{index}] 缺少 player_id")
    action = str(player.get("action") or "").strip()
    skill_names = [
        str(row.get("name") or "").strip()
        for row in player.get("skills") or []
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    selected_attribute = str(player.get("selected_attribute") or "").strip()
    selected_skill = str(player.get("selected_skill") or "").strip()

    label = f"{player_id}（{str(player.get('character_name') or player_id)}）"
    hint = f"玩家已显式选择：attribute={selected_attribute} skill={selected_skill}。" if (
        selected_attribute or selected_skill
    ) else ""
    questions: dict[str, dict[str, Any]] = {
        f"p{index}_needs_check": {
            "type": "noul",
            # OpenXLab 托管网关比官方 serving 严：noul 必须带 criteria 且键为 true/false
            # （实测 400 "field `criteria` must be an object with `true`/`false` keys"；
            # 官方规格允许省略或 yes/true/1 键，此形状两边兼容）
            "criteria": {"true": "需要系统检定", "false": "无需检定"},
            "instructions": f"{label} 的行动「{action[:300]}」是否需要系统检定？{hint}",
        },
        f"p{index}_kind": {
            "type": "choice",
            "instructions": f"{label} 该行动对应的检定类型是什么？",
            "criteria": {"check": "普通主动检定", "attack": "攻击", "save": "抵抗危险/豁免"},
        },
    }
    if dice_system == "d100":
        options = list(dict.fromkeys(skill_names + attributes))
        if not options:
            raise ParseError(f"players[{index}] 没有可用技能或属性")
        if len(options) > _MAX_OPTIONS:
            raise ParseError(f"players[{index}] 技能+属性数 {len(options)} 超过 62 选项上限")
        stat_criteria = {name: f"{name}（技能/属性）" for name in options}
        stat_instructions = f"{label} 该行动最应使用哪个技能或属性做检定？（技能优先）"
    else:
        stat_criteria = {key: key for key in attributes}
        stat_instructions = f"{label} 该行动最应使用哪个属性做检定？"
    questions[f"p{index}_stat"] = {
        "type": "choice",
        "instructions": stat_instructions,
        "criteria": stat_criteria,
    }
    if dice_system == "d20":
        # OpenXLab 托管网关要求 score 题 criteria 为档位数组（官方 serving 的键对象形状会被 400
        # "must be an array of levels"）；网关答案按索引给概率 + legend 映射，DC 数值由
        # build_response 侧按 argmax 档位还原。档位描述拼进 instructions。
        dc_pairs: list[tuple[int, str]] = []
        for key, value in dc_table.items():
            try:
                dc_pairs.append((int(value), str(key)))
            except (TypeError, ValueError):
                continue
        if not dc_pairs:
            raise ParseError("d20 规则集 dc_table 为空或非法")
        dc_pairs.sort()
        levels_desc = "／".join(f"DC{value}（{label}）" for value, label in dc_pairs)
        questions[f"p{index}_dc"] = {
            "type": "score",
            "instructions": f"{label} 该行动任务本身的客观难度（DC）是多少？档位：{levels_desc}",
            "criteria": [str(value) for value, _ in dc_pairs],
        }

    return {
        "player_id": player_id,
        "character_name": player.get("character_name"),
        "action": action,
        "skills": [{"name": name} for name in skill_names],
        "attribute_keys": list(attributes),
        "questions": questions,
    }
