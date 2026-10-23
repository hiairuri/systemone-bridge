"""北向编排：模型名路由 → 任务解析 → SystemOne 决策 → 响应合成，任一环失败直通回退。"""

from __future__ import annotations

import logging
from typing import Any

from .fallback import FallbackClient
from .parsers import NorthboundRequest, ParseError, TaskParser, extract_request
from .stats import BridgeStats
from .systemone import SystemOneClient, SystemOneError

logger = logging.getLogger("systemone_bridge.northbound")

# 北向模型名以此前缀路由进 SystemOne 翻译路径，其余一律直通
ROUTED_MODEL_PREFIX = "intern-decision"


class RoutingResult:
    """一次 /v1/chat/completions 的处理结果：非流式 JSON 响应体或流式字节迭代器。"""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        stream: Any = None,
        via: str = "",
    ) -> None:
        self.payload = payload
        self.stream = stream
        self.via = via


class NorthboundRouter:
    def __init__(
        self,
        parsers: list[TaskParser],
        systemone: SystemOneClient,
        fallback: FallbackClient,
        stats: BridgeStats | None = None,
    ) -> None:
        self._parsers = parsers
        self._systemone = systemone
        self._fallback = fallback
        self._stats = stats

    async def handle_chat_completions(self, body: dict[str, Any]) -> RoutingResult:
        model = str(body.get("model") or "")
        wants_stream = bool(body.get("stream"))
        self._record_request()
        if not model.startswith(ROUTED_MODEL_PREFIX):
            return await self._passthrough(body, via="passthrough.direct")
        request = extract_request(body)
        parser = self._match_parser(request)
        if parser is None:
            logger.info("model=%s 无解析器识别，直通回退", model)
            return await self._passthrough(body, via="passthrough.no_parser")
        try:
            parsed = parser.parse(request)
        except ParseError as exc:
            logger.warning("parser=%s 解析失败：%s；直通回退", parser.name, exc)
            self._record_error("passthrough.parse_error", "parse_error", f"{parser.name}: {exc}")
            return await self._passthrough(body, via="passthrough.parse_error")
        try:
            answers = await self._systemone.decide(parsed.state, parsed.questions)
        except SystemOneError as exc:
            logger.warning(
                "parser=%s SystemOne %s；直通回退", parser.name, exc
            )
            via = f"passthrough.systemone_{exc.category}"
            self._record_error(via, f"systemone_{exc.category}", f"{parser.name}: {exc}")
            return await self._passthrough(body, via=via)
        try:
            response_body = parser.build_response(request, parsed, answers)
        except ParseError as exc:
            logger.warning("parser=%s 响应合成失败：%s；直通回退", parser.name, exc)
            self._record_error("passthrough.build_error", "build_error", f"{parser.name}: {exc}")
            return await self._passthrough(body, via="passthrough.build_error")
        logger.info(
            "model=%s parser=%s 决策完成（via=systemone.%s, notes=%s）",
            model, parser.name, parser.name, parsed.notes,
        )
        via = f"systemone.{parser.name}"
        self._record_route(via)
        if wants_stream:
            return RoutingResult(
                stream=_single_chunk_stream(response_body),
                via=via,
            )
        return RoutingResult(payload=response_body, via=via)

    def _match_parser(self, request: NorthboundRequest) -> TaskParser | None:
        for parser in self._parsers:
            try:
                if parser.detect(request):
                    return parser
            except Exception:  # detect 异常等价于不识别，保持直通安全
                logger.exception("parser=%s detect 异常", getattr(parser, "name", "?"))
        return None

    def _record_request(self) -> None:
        """请求总数打点（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_request()

    def _record_route(self, via: str) -> None:
        """路由决策打点（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_route(via)

    def _record_error(self, via: str, kind: str, detail: str) -> None:
        """错误摘要打点（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_error(via, kind, detail)

    async def _passthrough(self, body: dict[str, Any], via: str) -> RoutingResult:
        self._record_route(via)
        if body.get("stream"):
            return RoutingResult(
                stream=self._fallback.forward_stream(body), via=via + ".stream"
            )
        payload = await self._fallback.forward(body)
        return RoutingResult(payload=payload, via=via)


async def _single_chunk_stream(response_body: dict[str, Any]) -> AsyncIterator[bytes]:
    """把非流式合成响应包装为两块 SSE（delta 语义），兼容 stream=true 的北向客户端。"""

    import json

    choices = response_body.get("choices") or []
    first_choices = []
    last_choices = []
    for choice in choices:
        message = choice.get("message") or {}
        delta: dict[str, Any] = {"role": message.get("role") or "assistant"}
        if message.get("content") is not None:
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = message["tool_calls"]
        first_choices.append(
            {"index": choice.get("index", 0), "delta": delta, "finish_reason": None}
        )
        last_choices.append(
            {"index": choice.get("index", 0), "delta": {}, "finish_reason": "stop"}
        )
    first = {
        "id": response_body.get("id"),
        "object": "chat.completion.chunk",
        "created": response_body.get("created"),
        "model": response_body.get("model"),
        "choices": first_choices,
    }
    last = {
        "id": response_body.get("id"),
        "object": "chat.completion.chunk",
        "created": response_body.get("created"),
        "model": response_body.get("model"),
        "choices": last_choices,
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8")
    yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"
