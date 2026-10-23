"""回退上游客户端：OpenAI 兼容 /chat/completions 直通代理。

架构铁律：解析失败/SystemOne 不可达/超时 → 原样直通本上游（默认 GLM），
任何情况下不比现状差。流式请求按 SSE 逐块透传。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

logger = logging.getLogger("systemone_bridge.fallback")

# 指向 intern-decision-* 的请求被迫回退时，改写为回退上游配置的模型
_ROUTED_PREFIX = "intern-decision"


class FallbackError(Exception):
    """回退上游自身失败——此时只能向客户端返回 502，不存在更优路径。"""


class FallbackClient:
    def __init__(self, config: Any, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session

    def _url(self) -> str:
        return self._config.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = str(getattr(self._config, "api_key", "") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def prepare_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """回退请求体：模型名缺失或指向 intern-decision-* 时改写为回退模型，其余原样。"""

        model = str(body.get("model") or "")
        if not model or model.startswith(_ROUTED_PREFIX):
            return {**body, "model": str(self._config.model)}
        return body

    async def forward(self, body: dict[str, Any]) -> dict[str, Any]:
        """非流式直通：返回上游原始响应 JSON。"""

        payload = self.prepare_body(body)
        timeout = aiohttp.ClientTimeout(total=float(self._config.timeout_seconds))
        try:
            async with self._session.post(
                self._url(), json=payload, headers=self._headers(), timeout=timeout
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise FallbackError(
                        f"status={response.status} body={text[:300]}"
                    )
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise FallbackError(f"非 JSON 响应：{exc}") from exc
        except aiohttp.ClientError as exc:
            raise FallbackError(str(exc)) from exc

    async def forward_stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        """流式直通：逐块产出上游 SSE 原始字节。"""

        payload = self.prepare_body(body)
        timeout = aiohttp.ClientTimeout(total=float(self._config.timeout_seconds))
        try:
            async with self._session.post(
                self._url(), json=payload, headers=self._headers(), timeout=timeout
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise FallbackError(f"status={response.status} body={text[:300]}")
                async for chunk in response.content.iter_any():
                    if chunk:
                        yield chunk
        except aiohttp.ClientError as exc:
            raise FallbackError(str(exc)) from exc
