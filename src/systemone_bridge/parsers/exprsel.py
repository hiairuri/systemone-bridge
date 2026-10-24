"""exprsel（MaiBot 表达方式选择）解析器。

模板来自 logs/maisaka_prompt/expression_selector/ 真实快照（1074 份）归纳：
- system 消息含「候选表达方式：」清单，行格式固定为 `id: 情景=… | 风格=…`；
- user 消息为聊天上下文（<message …> 行等），末条常为时间戳标记；
- 期望输出 `{"selected_ids":[123,456]}`（0–5 条）。

映射（本项目设计决策）：候选清单 → SystemOne 单题 choice（≤62 选项，实测候选 5–50 条）；
聊天上下文 → state；SystemOne 概率分布取 top-K（默认 ≤5、p ≥ 阈值）回填 selected_ids。
"""

from __future__ import annotations

import re
from typing import Any

from .base import (
    NorthboundRequest,
    ParsedTask,
    ParseError,
    chat_completion_response,
    dump_json,
)

# 候选行：`6845: 情景=狐系角色互动时 | 风格=句尾加 喵，自称本狐`
_CANDIDATE_LINE = re.compile(r"^(\d+):\s*情景=(.*?)\s*\|\s*风格=(.*)$", re.MULTILINE)
# 模板身份锚点（快照中两个版本都含此标记）
_SYSTEM_ANCHOR = "候选表达方式："

# SystemOne 单题选项硬上限（候选符号池 A..Z a..z 0..9）
_MAX_OPTIONS = 62


class ExpressionSelectorParser:
    name = "expression_selector"

    def __init__(self, min_probability: float = 0.05, max_selected: int = 5) -> None:
        self.min_probability = min_probability
        self.max_selected = max_selected

    def detect(self, request: NorthboundRequest) -> bool:
        # 保守识别：只认模板锚点；误识别会在 parse 阶段失败并返回 422 bad_template，无风险
        return _SYSTEM_ANCHOR in request.system_text

    def parse(self, request: NorthboundRequest) -> ParsedTask:
        candidates = [
            (int(match.group(1)), match.group(2).strip(), match.group(3).strip())
            for match in _CANDIDATE_LINE.finditer(request.system_text)
        ]
        if not candidates:
            raise ParseError("候选表达方式清单为空或格式不匹配")
        # 同一 id 重复出现时保留首个（快照未观察到重复，防御性处理）
        unique: dict[int, tuple[str, str]] = {}
        for expr_id, scene, style in candidates:
            unique.setdefault(expr_id, (scene, style))
        if len(unique) > _MAX_OPTIONS:
            raise ParseError(f"候选数 {len(unique)} 超过 SystemOne 单题 62 选项上限")
        criteria = {
            str(expr_id): f"情景={scene} | 风格={style}"
            for expr_id, (scene, style) in unique.items()
        }
        state = {
            "task": "从候选表达方式中为本次可见回复挑选最合适的条目；若都不合适则视为空选择。",
            "recent_chat": request.user_texts,
        }
        questions = {
            "selection": {
                "type": "choice",
                "instructions": (
                    "哪条候选表达方式最适合当前语境？概率越低代表越不合适；"
                    "全部都不合适时各选项概率应接近均匀且偏低。"
                ),
                "criteria": criteria,
            }
        }
        return ParsedTask(
            kind=self.name,
            state=state,
            questions=questions,
            notes=[f"候选 {len(unique)} 条"],
        )

    def build_response(
        self, request: NorthboundRequest, parsed: ParsedTask, answers: dict[str, Any]
    ) -> dict[str, Any]:
        answer = answers.get("selection")
        if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
            raise ParseError("SystemOne 响应缺少 selection 概率分布")
        ranked = sorted(
            answer["probabilities"].items(), key=lambda item: (-item[1], item[0])
        )
        # 候选 id 在 criteria 键里是字符串，回填模板输出格式时转回整数
        selected = [
            int(expr_id)
            for expr_id, probability in ranked[: self.max_selected]
            if probability >= self.min_probability
        ]
        content = dump_json({"selected_ids": selected})
        return chat_completion_response(request.model, content)
