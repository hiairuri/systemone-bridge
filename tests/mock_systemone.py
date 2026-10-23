"""按 P1 规格实现的 mock SystemOne 服务（aiohttp），供测试使用。

只实现桥所依赖的契约面：POST /v1/decisions（校验 + 返回 Jev 包络）、GET /health。
概率分布由可注入的 handler 决定；默认 handler 给出确定性分布（首选项 0.9）。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Callable

from aiohttp import web

Handler = Callable[[dict[str, Any]], dict[str, Any] | web.Response]

VALID_TYPES = {"choice", "score", "noul"}


def default_handler(request: dict[str, Any]) -> dict[str, Any]:
    """确定性默认分布：choice/score 首选项 0.9，noul yes=0.9。"""

    answers: dict[str, Any] = {}
    for name, question in request["questions"].items():
        kind = question["type"]
        if kind == "noul":
            answers[name] = _noul_answer(0.9)
        else:
            options = list(question.get("criteria") or {})
            answers[name] = _choice_answer(kind, options, options[0] if options else "")
    return answers


def _choice_answer(kind: str, options: list[str], best: str) -> dict[str, Any]:
    probabilities = {option: (0.9 if option == best else 0.1 / max(len(options) - 1, 1)) for option in options}
    answer: dict[str, Any] = {
        "type": kind,
        "probabilities": probabilities,
        "confidence": probabilities[best],
        "decision": best,
        "source": "local",
    }
    if kind == "choice":
        answer["choice"] = best
    elif kind == "score":
        answer["score"] = sum(float(k) * p for k, p in probabilities.items())
        answer["legend"] = dict(probabilities)
    return answer


def _noul_answer(p_yes: float) -> dict[str, Any]:
    probabilities = {"no": 1.0 - p_yes, "yes": p_yes}
    decision = "yes" if p_yes >= 0.5 else "no"
    return {
        "type": "noul",
        "probabilities": probabilities,
        "confidence": max(probabilities.values()),
        "decision": decision,
        "noul": p_yes,
        "source": "local",
    }


def validate_like_official(request: dict[str, Any]) -> str | None:
    """按官方 validate_request 的核心约束做校验，返回错误原因或 None。"""

    if not isinstance(request, dict) or "state" not in request:
        return "Supply a JSON object containing state and questions."
    questions = request.get("questions")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 16:
        return "Supply 1–16 questions."
    for name, question in questions.items():
        if not isinstance(name, str) or not name:
            return "Question names must be nonempty strings."
        if not isinstance(question, dict) or question.get("type") not in VALID_TYPES:
            return "Question type must be choice, score, or noul."
        criteria = question.get("criteria")
        if question["type"] == "choice" and (not isinstance(criteria, dict) or not criteria):
            return "Choice criteria must be an object."
        if question["type"] == "score" and not isinstance(criteria, (list, dict)):
            return "Score criteria must be a list or object."
        if question["type"] != "noul" and (not isinstance(criteria, (list, dict)) or not criteria):
            return "Supply 1–62 options."
    state_text = json.dumps(request["state"], ensure_ascii=False)
    if "<decision>" in state_text or "<decision>" in json.dumps(questions, ensure_ascii=False):
        return "Reserved decision marker appears in input evidence"
    return None


def create_mock_systemone_app(
    handler: Handler | None = None,
) -> web.Application:
    """handler 可返回 answers dict（默认包络包装）或直接返回 web.Response。"""

    handler = handler or default_handler

    async def decisions(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"detail": "invalid JSON"}, status=422)
        error = validate_like_official(body)
        if error:
            return web.json_response({"detail": error}, status=422)
        result = handler(body)
        if isinstance(result, web.Response):
            return result
        return web.json_response(
            {
                "model": "intern-decision",
                "request_id": secrets.token_hex(16),
                "answers": result,
                "usage": {
                    "input_tokens": 128,
                    "output_tokens": len(result),
                    "decision_count": len(result),
                },
                "timing": {"queue_ms": 0.1, "server_ms": 1.0, "inference_ms": 0.9},
                "thinking": {"enabled": False, "confidence_threshold": 0.5, "tasks": []},
                "backend": "mock",
            }
        )

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "model_loaded": True})

    app = web.Application()
    app.router.add_post("/v1/decisions", decisions)
    app.router.add_get("/health", health)
    return app


# ---- 测试工具：把 aiohttp app 起在随机空闲端口并返回 base_url + runner ----


async def spawn(app: web.Application) -> tuple[str, web.AppRunner]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    return f"http://{host}:{port}", runner
