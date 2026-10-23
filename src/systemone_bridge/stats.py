"""运行统计：请求总数、路由命中计数、回退计数、最近错误摘要（供 /api/status）。

线程安全说明：全部调用发生在 aiohttp 单线程事件循环内，普通属性即可。
"""

from __future__ import annotations

import time
from collections import Counter, deque
from typing import Any

# 最近错误环形缓冲容量（/api/status 只回最近若干条，留余量）
_MAX_RECENT_ERRORS = 50
# 单条错误详情截断长度（避免把上游大响应体塞进状态接口）
_DETAIL_MAX = 200


class BridgeStats:
    """桥运行统计聚合器：由北向处理器在每次请求路径上打点。"""

    def __init__(self) -> None:
        self._started_mono = time.monotonic()
        self._started_wall = time.time()
        self.total_requests = 0
        # via → 次数（如 systemone.expression_selector / passthrough.direct）
        self._route_counter: Counter[str] = Counter()
        self._recent_errors: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT_ERRORS)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_mono

    @property
    def started_at(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._started_wall))

    def record_request(self) -> None:
        self.total_requests += 1

    def record_route(self, via: str) -> None:
        """记录一次路由决策（via 前缀 systemone.* 命中翻译路径，passthrough.* 为回退）。"""

        self._route_counter[via] += 1

    def record_error(self, via: str, kind: str, detail: str) -> None:
        """记录一条错误摘要（解析失败/上游失败等），超长详情截断。"""

        self._recent_errors.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "via": via,
                "kind": kind,
                "detail": detail[:_DETAIL_MAX],
            }
        )

    @property
    def systemone_hits(self) -> int:
        return sum(count for via, count in self._route_counter.items() if via.startswith("systemone."))

    @property
    def fallback_hits(self) -> int:
        return sum(count for via, count in self._route_counter.items() if via.startswith("passthrough."))

    def snapshot(self, recent_limit: int = 10) -> dict[str, Any]:
        """导出 /api/status 所需的统计快照。"""

        return {
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "total_requests": self.total_requests,
            "systemone_hits": self.systemone_hits,
            "fallback_hits": self.fallback_hits,
            "routes": dict(self._route_counter.most_common()),
            "recent_errors": list(self._recent_errors)[-recent_limit:],
        }
