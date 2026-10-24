"""任务解析器公共接口：识别北向请求 → 产出 SystemOne 结构化决策任务。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class ParseError(Exception):
    """模板识别成功但结构化解析失败。北向统一映射为 422 bad_template。"""


@dataclass
class ParsedTask:
    """一次可交给 SystemOne /v1/decisions 的结构化决策任务。"""

    kind: str
    state: Any
    questions: dict[str, dict[str, Any]]
    # 解析过程中的说明（进入日志，不进协议）
    notes: list[str] = field(default_factory=list)


@dataclass
class NorthboundRequest:
    """北向 /v1/chat/completions 请求的已提取视图。"""

    body: dict[str, Any]
    model: str
    system_text: str
    user_texts: list[str]
    tools: list[dict[str, Any]]

    @property
    def user_text(self) -> str:
        return "\n".join(self.user_texts)


def extract_request(body: dict[str, Any]) -> NorthboundRequest:
    """从 OpenAI chat.completions 请求体提取 system 文本、user 文本列表与工具定义。"""

    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ParseError("messages 缺失或不是数组")
    system_parts: list[str] = []
    user_texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")
        if role == "system":
            system_parts.append(_content_text(content))
        elif role == "user":
            user_texts.append(_content_text(content))
    tools = [tool for tool in body.get("tools") or [] if isinstance(tool, dict)]
    model = str(body.get("model") or "")
    return NorthboundRequest(
        body=body, model=model, system_text="\n".join(system_parts),
        user_texts=user_texts, tools=tools,
    )


def _content_text(content: Any) -> str:
    """OpenAI content 可能是字符串，也可能是分片数组；只取文本分片。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return ""


def tool_names(request: NorthboundRequest) -> set[str]:
    """收集请求里声明的函数工具名（OpenAI tools 形状）。"""

    names = set()
    for tool in request.tools:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        name = (function or {}).get("name") or tool.get("name")
        if name:
            names.add(str(name))
    return names


class TaskParser(Protocol):
    """任务解析器协议：detect 必须廉价且保守，parse 失败抛 ParseError。"""

    name: str

    def detect(self, request: NorthboundRequest) -> bool: ...

    def parse(self, request: NorthboundRequest) -> ParsedTask: ...

    def build_response(
        self, request: NorthboundRequest, parsed: ParsedTask, answers: dict[str, Any]
    ) -> dict[str, Any]:
        """把 SystemOne answers 合成为北向 OpenAI chat.completion 响应体。"""


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
