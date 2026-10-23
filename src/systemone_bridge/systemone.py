"""南向 SystemOne（Intern-Decision /v1/decisions）客户端。

错误分类（调用方统一按「直通回退」处理）：
- unreachable：连接失败/超时/DNS 失败；
- server_error：5xx（含官方 503 not ready）；
- rejected：4xx——请求被端点拒绝（含 422 校验失败），翻译产物可能不合法，同样回退；
- bad_response：2xx 但响应体不符合 Jev 包络。

协议依据：specs/systemone-protocol.md。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger("systemone_bridge.systemone")


class SystemOneError(Exception):
    def __init__(self, category: str, detail: str = "") -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"[{category}] {detail}")


class SystemOneClient:
    def __init__(self, config: Any, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session

    def update_config(self, config: Any) -> None:
        """热应用新配置（WebUI 保存后对后续请求立即生效，不重启）。"""

        self._config = config

    def _url(self) -> str:
        return self._config.base_url.rstrip("/") + "/v1/decisions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = str(getattr(self._config, "api_key", "") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def health(self) -> dict[str, Any] | None:
        """探测 /health（可选诊断用）；失败返回 None，不抛错。"""

        try:
            url = self._config.base_url.rstrip("/") + "/health"
            async with self._session.get(
                url, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
                return data if isinstance(data, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def decide(
        self,
        state: Any,
        questions: dict[str, dict[str, Any]],
        images: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """调用 /v1/decisions，成功返回 answers 字典；任何失败抛 SystemOneError。"""

        payload: dict[str, Any] = {"state": state, "questions": questions}
        if images:
            payload["images"] = images
        timeout = aiohttp.ClientTimeout(total=float(self._config.timeout_seconds))
        try:
            async with self._session.post(
                self._url(), json=payload, headers=self._headers(), timeout=timeout
            ) as response:
                body_text = await response.text()
                if response.status == 200:
                    return self._extract_answers(body_text)
                detail = body_text[:300]
                if response.status >= 500:
                    raise SystemOneError(
                        "server_error", f"status={response.status} body={detail}"
                    )
                raise SystemOneError(
                    "rejected", f"status={response.status} body={detail}"
                )
        except SystemOneError:
            raise
        except asyncio.TimeoutError:
            raise SystemOneError("unreachable", f"timeout after {timeout.total}s")
        except aiohttp.ClientError as exc:
            raise SystemOneError("unreachable", str(exc))

    @staticmethod
    def _extract_answers(body_text: str) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise SystemOneError("bad_response", f"非 JSON 响应：{exc}") from exc
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict) or not answers:
            raise SystemOneError("bad_response", "响应缺少非空 answers 字段")
        for name, answer in answers.items():
            if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
                raise SystemOneError("bad_response", f"answers.{name} 缺少 probabilities")
        return answers
