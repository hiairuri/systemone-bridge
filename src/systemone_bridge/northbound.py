"""北向编排：任务解析 → SystemOne 决策 → 响应合成（纯协议转换器，无回退）。

任何一环失败都直接抛 BridgeError，由 server 层渲染 OpenAI 风格错误体；
回退逻辑由调用方自行负责。
"""

from __future__ import annotations

import logging
from typing import Any

from .parsers import NorthboundRequest, ParseError, TaskParser, extract_request
from .stats import BridgeStats
from .systemone import SystemOneClient, SystemOneError

logger = logging.getLogger("systemone_bridge.northbound")


class BridgeError(Exception):
    """北向处理失败：携带 HTTP 状态码与 OpenAI 风格错误码，由 server 层统一渲染。"""

    def __init__(self, status: int, code: str, message: str, error_type: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.error_type = error_type
        super().__init__(f"[{status}/{code}] {message}")


def _openai_usage(upstream: dict[str, Any] | None) -> dict[str, int] | None:
    """把上游 SystemOne usage 透传为 OpenAI usage 形状。

    input_tokens→prompt_tokens、output_tokens→completion_tokens（上游 output 计打分
    字段数而非生成 token，见 specs/systemone-protocol.md §4）；上游未回报 usage 或两
    计数皆缺时返回 None，北向响应不携带 usage 字段（与 OpenAI 语义一致：字段可选）。
    """

    if not isinstance(upstream, dict):
        return None
    prompt = int(upstream.get("input_tokens") or 0)
    completion = int(upstream.get("output_tokens") or 0)
    if prompt <= 0 and completion <= 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


class NorthboundRouter:
    def __init__(
        self,
        parsers: list[TaskParser],
        systemone: SystemOneClient,
        stats: BridgeStats | None = None,
    ) -> None:
        self._parsers = parsers
        self._systemone = systemone
        self._stats = stats

    @property
    def parser_names(self) -> list[str]:
        """已启用解析器名清单（/health 汇报用）。"""

        return [parser.name for parser in self._parsers]

    async def handle_chat_completions(self, body: dict[str, Any]) -> dict[str, Any]:
        """处理一次 /v1/chat/completions：纯协议转换，成功返回 OpenAI 响应体。"""

        model = str(body.get("model") or "")
        self._record_request()
        if body.get("stream"):
            raise BridgeError(
                400,
                "streaming_not_supported",
                "systemone-bridge 是纯协议转换器，不支持流式响应（stream=true）",
                "invalid_request_error",
            )
        try:
            request = extract_request(body)
        except ParseError as exc:
            raise BridgeError(400, "invalid_request", str(exc), "invalid_request_error")
        parser = self._match_parser(request)
        if parser is None:
            # 混合 model_list 架构下的常态预期流量（生成型任务 422 后由调用方切下一模型），
            # 计入 not_decisions（不进失败/错误流），debug 日志不刷屏。
            logger.debug("model=%s 无解析器锚点，返回 422 unknown_template", model)
            self._record_not_decision(f"model={model} 不匹配任何已启用解析器")
            raise BridgeError(
                422,
                "unknown_template",
                f"请求不匹配任何已启用的任务解析器（model={model}）",
                "invalid_request_error",
            )
        try:
            parsed = parser.parse(request)
        except ParseError as exc:
            logger.warning("parser=%s 解析失败：%s；返回 422 bad_template", parser.name, exc)
            self._record_error("bad_template", f"{parser.name}: {exc}")
            raise BridgeError(
                422, "bad_template", f"模板识别成功但解析失败（{parser.name}）：{exc}",
                "invalid_request_error",
            )
        try:
            reply = await self._systemone.decide(parsed.state, parsed.questions)
        except SystemOneError as exc:
            # 原四分类（unreachable/server_error/rejected/bad_response）并入错误 message
            logger.warning("parser=%s SystemOne %s；返回 502 upstream_error", parser.name, exc)
            self._record_error("upstream_error", f"{parser.name}: {exc}")
            raise BridgeError(
                502, "upstream_error", f"SystemOne 决策调用失败：{exc}", "api_error"
            )
        try:
            response_body = parser.build_response(request, parsed, reply.answers)
        except ParseError as exc:
            logger.warning("parser=%s 响应合成失败：%s；返回 500", parser.name, exc)
            self._record_error("build_error", f"{parser.name}: {exc}")
            raise BridgeError(
                500, "build_error", f"决策结果合成响应失败（{parser.name}）：{exc}", "api_error"
            )
        usage = _openai_usage(reply.usage)
        if usage is not None:
            # 上游 usage 真实透传（input→prompt、output→completion），供调用方 token 统计
            response_body["usage"] = usage
        logger.info(
            "model=%s parser=%s 决策完成（via=systemone.%s, notes=%s）",
            model, parser.name, parser.name, parsed.notes,
        )
        self._record_route(f"systemone.{parser.name}")
        return response_body

    def _match_parser(self, request: NorthboundRequest) -> TaskParser | None:
        for parser in self._parsers:
            try:
                if parser.detect(request):
                    return parser
            except Exception:  # detect 异常等价于不识别，进入 unknown_template
                logger.exception("parser=%s detect 异常", getattr(parser, "name", "?"))
        return None

    def _record_request(self) -> None:
        """请求总数打点（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_request()

    def _record_route(self, via: str) -> None:
        """决策成功打点（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_route(via)

    def _record_error(self, code: str, detail: str) -> None:
        """错误打点：按错误码计数并记入最近错误（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_error(code, detail)

    def _record_not_decision(self, detail: str) -> None:
        """非决策请求打点：预期流量，单独计数（未接统计器时为空操作）。"""

        if self._stats is not None:
            self._stats.record_not_decision()
