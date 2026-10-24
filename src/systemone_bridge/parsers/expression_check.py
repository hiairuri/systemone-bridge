"""expression_check（MaiBot 表达方式入库前审核）解析器。

模板来自 prompts/zh-CN/expression_evaluation.prompt 全文与调用方
src/learners/expression_utils.py（judge_llm，request_type=expression.check）亲读：
- 单 prompt 经 LLMServiceClient.generate_response 以**单条 user 消息**北向（无 system 前导）；
- 模板固定行「使用条件或使用情景：{situation}」「表达方式或言语风格：{style}」（首行是
  「语言风格」而模板第三行是「言语风格」，两词不同，锚点互不干扰）；
- 期望输出 JSON `{"suitable": bool, "reason": 文本}`，调用方 parse_evaluation_response
  兼容裸 JSON/代码围栏/正则兜底三种形状。

映射（本项目设计决策）：审核判定 → SystemOne 单题 noul（yes=适合入库）；
表达内容+情景 → 结构化 state；P(yes) ≥ 0.5 → suitable=true。
review_logs.json（12,573 条）确认 situation/style 均为单行短文本。
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
    full_text,
)

# 模板身份锚点（全 prompts 树仅 expression_evaluation.prompt 含此句）
_TEMPLATE_ANCHOR = "请评估以下表达方式或语言风格"
# 字段行（冒号后到行尾即字段值；首行「…使用条件或使用情景是否合适：」因多出「是否合适」不会误命中）
_SITUATION_LINE = re.compile(r"使用条件或使用情景：(.*)")
_STYLE_LINE = re.compile(r"表达方式或言语风格：(.*)")

# 调用方 expression_utils.check_expression_suitability 内固定的 4 条审核标准
_CRITERIA = [
    "表达方式或言语风格是否与使用条件或使用情景匹配",
    "允许部分语法错误或口语化或缺省出现",
    "表达方式不能太过特指，需要具有泛用性",
    "一般不涉及具体的人名或名称",
]

_YES_THRESHOLD = 0.5


class ExpressionCheckParser:
    name = "expression_check"

    def detect(self, request: NorthboundRequest) -> bool:
        # 保守识别：只认模板锚点；识别后 parse 失败会走 422 bad_template，无风险
        return _TEMPLATE_ANCHOR in full_text(request)

    def parse(self, request: NorthboundRequest) -> ParsedTask:
        text = full_text(request)
        situation_match = _SITUATION_LINE.search(text)
        style_match = _STYLE_LINE.search(text)
        if situation_match is None or style_match is None:
            raise ParseError("模板缺少「使用条件或使用情景：」或「表达方式或言语风格：」字段行")
        situation = situation_match.group(1).strip()
        style = style_match.group(1).strip()
        if not situation or not style:
            raise ParseError("situation/style 字段值为空")

        state = {
            "task": "判断该表达方式或言语风格是否适合加入表达库（审核标准见 criteria）。",
            "situation": situation,
            "style": style,
            "criteria": list(_CRITERIA),
        }
        questions = {
            "suitability": {
                "type": "noul",
                # OpenXLab 托管网关比官方 serving 严：noul 必须带 criteria 且键为 true/false
                # （实测 400 "field `criteria` must be an object with `true`/`false` keys"；
                # 官方规格允许省略或 yes/true/1 键，此形状两边兼容）
                "criteria": {"true": "适合入库", "false": "不适合入库"},
                "instructions": (
                    "该表达方式或言语风格在给定使用情景下是否适合入库？"
                    "yes=适合（与情景匹配、泛用、不特指具体人名）；no=不适合。"
                ),
            }
        }
        return ParsedTask(
            kind=self.name,
            state=state,
            questions=questions,
            notes=[f"situation={situation[:40]!r} style={style[:40]!r}"],
        )

    def build_response(
        self, request: NorthboundRequest, parsed: ParsedTask, answers: dict[str, Any]
    ) -> dict[str, Any]:
        answer = answers.get("suitability")
        if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
            raise ParseError("SystemOne 响应缺少 suitability 概率分布")
        # noul 协议固定 {"yes": p, "no": 1-p}；P(yes) 即适合入库概率
        p_yes = float(answer["probabilities"].get("yes") or 0.0)
        suitable = p_yes >= _YES_THRESHOLD
        reason = f"决策模型判定{'适合' if suitable else '不适合'}（P(适合)={p_yes:.2f}）"
        content = dump_json({"suitable": suitable, "reason": reason})
        return chat_completion_response(request.model, content)
