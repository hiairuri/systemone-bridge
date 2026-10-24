"""jargon_compare（MaiBot 黑话推断比对）解析器。

模板来自 prompts/zh-CN/jargon_compare_inference.prompt 全文与调用方
src/learners/jargon_miner.py（步骤3，llm_inference 单 prompt 北向）亲读：
- 模板含两个固定标题「**推断结果1（基于上下文）**」「**推断结果2（仅基于词条）**」，
  其后各跟一段内嵌 JSON 推断原文（真实快照 jargon_learning_update/…/1792592317215.json 亲读）；
- 期望输出 JSON `{"is_similar": bool, "reason": 文本}`，调用方 _parse_result
  （json.loads + repair_json 兜底）只消费 is_similar（jargon_miner.py:531-532，
  is_jargon = not is_similar），reason 仅透传 hook。

映射（本项目设计决策）：同义判定 → SystemOne 单题 noul（yes=相同或类似）；
两段推断原文（尽量解析为对象）→ 结构化 state；P(yes) ≥ 0.5 → is_similar=true。
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    NorthboundRequest,
    ParsedTask,
    ParseError,
    chat_completion_response,
    dump_json,
    full_text,
)

# 模板身份锚点（全 prompts 树仅 jargon_compare_inference.prompt 含此句）
_TEMPLATE_ANCHOR = "请比较这两个推断结果"
# 两个推断段的标题锚点（模板固定，快照确认渲染后原样保留）
_HEAD1 = "**推断结果1（基于上下文）**"
_HEAD2 = "**推断结果2（仅基于词条）**"

_YES_THRESHOLD = 0.5


def _parse_inference(raw: str) -> Any:
    """推断段是内嵌 JSON 原文；解析成功给对象（state 结构化），失败保留原文不致命。"""

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return value if isinstance(value, dict) else raw


class JargonCompareParser:
    name = "jargon_compare"

    def detect(self, request: NorthboundRequest) -> bool:
        # 保守识别：只认模板锚点；识别后 parse 失败会走 422 bad_template，无风险
        return _TEMPLATE_ANCHOR in full_text(request)

    def parse(self, request: NorthboundRequest) -> ParsedTask:
        text = full_text(request)
        head1 = text.find(_HEAD1)
        head2 = text.find(_HEAD2)
        tail = text.find(_TEMPLATE_ANCHOR)
        if head1 == -1 or head2 == -1 or tail == -1 or not head1 < head2 < tail:
            raise ParseError("模板缺少两个推断结果标题或顺序异常")
        inference1_raw = text[head1 + len(_HEAD1) : head2].strip()
        inference2_raw = text[head2 + len(_HEAD2) : tail].strip()
        if not inference1_raw or not inference2_raw:
            raise ParseError("推断结果1/2 内容为空")

        state = {
            "task": "比较两段黑话含义推断结果，判断主要含义是否相同或类似（忽略细微表述差异）。",
            "inference1": _parse_inference(inference1_raw),
            "inference2": _parse_inference(inference2_raw),
        }
        questions = {
            "similarity": {
                "type": "noul",
                # OpenXLab 托管网关比官方 serving 严：noul 必须带 criteria 且键为 true/false
                # （实测 400 "field `criteria` must be an object with `true`/`false` keys"；
                # 官方规格允许省略或 yes/true/1 键，此形状两边兼容）
                "criteria": {"true": "相同或类似", "false": "有实质差异"},
                "instructions": (
                    "两段推断结果对同一词条的主要含义是否相同或类似？"
                    "yes=相同或类似（仅表述详略不同也算）；no=有实质含义差异。"
                ),
            }
        }
        return ParsedTask(
            kind=self.name,
            state=state,
            questions=questions,
            notes=["两段推断长度=%d/%d" % (len(inference1_raw), len(inference2_raw))],
        )

    def build_response(
        self, request: NorthboundRequest, parsed: ParsedTask, answers: dict[str, Any]
    ) -> dict[str, Any]:
        answer = answers.get("similarity")
        if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
            raise ParseError("SystemOne 响应缺少 similarity 概率分布")
        # noul 协议固定 {"yes": p, "no": 1-p}；P(yes) 即「相同或类似」概率
        p_yes = float(answer["probabilities"].get("yes") or 0.0)
        is_similar = p_yes >= _YES_THRESHOLD
        reason = f"决策模型判定{'相同或类似' if is_similar else '存在实质差异'}（P(相似)={p_yes:.2f}）"
        content = dump_json({"is_similar": is_similar, "reason": reason})
        return chat_completion_response(request.model, content)
