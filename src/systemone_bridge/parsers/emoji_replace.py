r"""emoji_replace（MaiBot 表情包替换决策）解析器。

模板来自 prompts/zh-CN/emoji_replace.prompt 全文与调用方
src/emoji_system/emoji_manager.py:884-945 亲读（该请求不落 maisaka_prompt 快照，
形状判定基于模板+源码）：
- 单 prompt 经 LLMServiceClient.generate_response 以**单条 user 消息**北向；
- 现有表情包列表由 emoji_manager 拼装，行格式固定：
  `编号: N\n描述: …\n使用次数: …\n添加时间: …`（候选 ≤ MAX_EMOJI_FOR_PROMPT=20 条）；
- 期望输出**自由文本**：`不取消注册` 或 `取消注册编号X`——调用方先按 skip_decisions
  子串匹配，再按正则 `(?:取消注册|删除)编号(\d+)` 提取编号（emoji_manager.py:916-922）。

映射（本项目设计决策）：替换决策 → SystemOne 单题 choice（候选编号 + keep，
≤21 选项，远低于 62 上限）；响应按调用方期望的文本格式合成（非 JSON）。
"""

from __future__ import annotations

import re
from typing import Any

from .base import (
    NorthboundRequest,
    ParsedTask,
    ParseError,
    chat_completion_response,
    full_text,
)

# 模板身份锚点（全 prompts 树仅 emoji_replace.prompt 含此句）
_TEMPLATE_ANCHOR = "需要决定是否取消注册一个旧表情包"
_LIST_ANCHOR = "现有表情包列表："
# 候选条目块：编号/描述/使用次数/添加时间 四行连续（emoji_manager f-string 拼装）
_ENTRY_LINE = re.compile(
    r"^编号: (\d+)\n描述: ([^\n]*)\n使用次数: (\d+)\n添加时间: ([^\n]*)",
    re.MULTILINE,
)
_NEW_EMOJI_DESC = re.compile(r"新表情包信息：\n描述: ([^\n]*)")

# SystemOne 单题选项硬上限（候选编号 + keep）
_MAX_OPTIONS = 62
_KEEP_KEY = "keep"


class EmojiReplaceParser:
    name = "emoji_replace"

    def detect(self, request: NorthboundRequest) -> bool:
        # 保守识别：双锚点同时命中才算（模板唯一，宁漏勿错）
        text = full_text(request)
        return _TEMPLATE_ANCHOR in text and _LIST_ANCHOR in text

    def parse(self, request: NorthboundRequest) -> ParsedTask:
        text = full_text(request)
        list_section = text.split(_LIST_ANCHOR, 1)[1]
        entries = [
            (int(match.group(1)), match.group(2).strip(), int(match.group(3)), match.group(4).strip())
            for match in _ENTRY_LINE.finditer(list_section)
        ]
        if not entries:
            raise ParseError("现有表情包列表为空或条目格式不匹配（编号/描述/使用次数/添加时间）")
        # 同一编号重复出现时保留首个（源码按 i+1 生成不会重复，防御性处理）
        unique: dict[int, tuple[str, int, str]] = {}
        for number, description, query_count, registered_at in entries:
            unique.setdefault(number, (description, query_count, registered_at))
        if len(unique) + 1 > _MAX_OPTIONS:
            raise ParseError(f"候选数 {len(unique)} 超过 SystemOne 单题 62 选项上限")

        new_desc_match = _NEW_EMOJI_DESC.search(text)
        new_emoji_description = new_desc_match.group(1).strip() if new_desc_match else ""

        criteria = {
            str(number): (
                f"取消注册编号{number}：{description}"
                f"（使用{query_count}次，添加于{registered_at}）"
            )
            for number, (description, query_count, registered_at) in unique.items()
        }
        criteria[_KEEP_KEY] = "不取消注册：保留全部现有表情包，放弃新表情包"
        state = {
            "task": (
                "表情包库已满，需决定是否取消注册一个旧表情包来为新表情包腾出名额；"
                "若不取消任何旧表情包应选 keep。"
            ),
            "new_emoji_description": new_emoji_description,
            "candidates": [
                {
                    "number": number,
                    "description": description,
                    "query_count": query_count,
                    "registered_at": registered_at,
                }
                for number, (description, query_count, registered_at) in unique.items()
            ],
        }
        questions = {
            "replacement": {
                "type": "choice",
                "instructions": (
                    "要为新表情包腾出名额时，应取消注册哪个现有表情包？"
                    "概率越低代表越不该取消该条目（常用/新奇/语义与新版图相近的优先保留）；"
                    "若不值得取消任何旧表情包，keep 的概率应最高。"
                ),
                "criteria": criteria,
            }
        }
        return ParsedTask(
            kind=self.name,
            state=state,
            questions=questions,
            notes=[f"候选 {len(unique)} 条，新表情描述={new_emoji_description[:40]!r}"],
        )

    def build_response(
        self, request: NorthboundRequest, parsed: ParsedTask, answers: dict[str, Any]
    ) -> dict[str, Any]:
        answer = answers.get("replacement")
        if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
            raise ParseError("SystemOne 响应缺少 replacement 概率分布")
        probabilities: dict[str, Any] = answer["probabilities"]
        criteria = parsed.questions["replacement"]["criteria"]
        # 端点给出合法 decision 时直接采信，否则按概率 argmax（criteria 键序稳定破平）
        decision = str(answer.get("decision") or "")
        if decision not in criteria:
            decision = max(criteria, key=lambda key: float(probabilities.get(key) or 0.0))
        # 调用方（emoji_manager.py:916-922）按自由文本子串+正则解析，必须逐字匹配这两种形状
        content = "不取消注册" if decision == _KEEP_KEY else f"取消注册编号{decision}"
        return chat_completion_response(request.model, content)
