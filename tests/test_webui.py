"""WebUI 测试：配置脱敏、掩码占位保留旧密钥、校验失败不落盘、保存热生效、连通性测试端点、运行状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from aiohttp import ClientSession

from conftest import create_mock_fallback_app, exprsel_request, make_stack_config, spawn
from mock_systemone import create_mock_systemone_app
from systemone_bridge.config import BridgeConfig, bootstrap_config_file, dump_config_toml, load_config
from systemone_bridge.server import create_app

# 已知真实密钥：所有断言都验证「完整密钥不外泄、掩码提交保留旧值」
SECRET_KEY = "sk-webui-secret-key-987654321"
MASKED_KEY = SECRET_KEY[:4] + "***"


@dataclass
class WebuiStack:
    bridge_url: str
    config_path: Path
    fallback_calls: list[dict[str, Any]] = field(default_factory=list)
    _runners: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        for runner in self._runners:
            await runner.cleanup()


@pytest.fixture
async def webui_stack(tmp_path: Path, http_client: ClientSession) -> AsyncIterator[WebuiStack]:
    calls: list[dict[str, Any]] = []
    fallback_url, runner1 = await spawn(create_mock_fallback_app(calls))
    systemone_url, runner2 = await spawn(create_mock_systemone_app())
    config = make_stack_config(systemone_url, fallback_url + "/v1")
    config.fallback.api_key = SECRET_KEY
    config_path = tmp_path / "config.toml"
    async with create_app(config, tmp_path, config_path) as bridge_app:
        bridge_url, runner3 = await spawn(bridge_app)
        stack = WebuiStack(
            bridge_url=bridge_url, config_path=config_path,
            fallback_calls=calls, _runners=[runner1, runner2, runner3],
        )
        try:
            yield stack
        finally:
            await stack.aclose()


async def get_config(client: ClientSession, base: str) -> dict[str, Any]:
    async with client.get(f"{base}/api/config") as response:
        assert response.status == 200
        return await response.json()


async def post_config(client: ClientSession, base: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with client.post(f"{base}/api/config", json=body) as response:
        return response.status, await response.json()


# ---------- 首页与脱敏 ----------


async def test_index_page_served(webui_stack: WebuiStack, http_client: ClientSession) -> None:
    async with http_client.get(webui_stack.bridge_url + "/") as response:
        assert response.status == 200
        assert response.content_type == "text/html"
        text = await response.text()
    assert "systemone-bridge" in text
    assert "保存配置" in text  # 中文单页：卡片 + 保存栏


async def test_get_config_masks_api_key(webui_stack: WebuiStack, http_client: ClientSession) -> None:
    async with http_client.get(webui_stack.bridge_url + "/api/config") as response:
        raw = await response.text()
        data = await response.json(content_type=None)
    # 完整密钥绝不出现，掩码为「前 4 字符 + ***」
    assert SECRET_KEY not in raw
    assert data["fallback"]["api_key"] == MASKED_KEY
    assert data["upstream_systemone"]["api_key"] == ""  # 空密钥保持空


def test_dump_config_toml_roundtrip(tmp_path: Path) -> None:
    """保存序列化与加载互为逆操作（保存落盘的内容必须能原样读回）。"""

    config = BridgeConfig()
    config_path = tmp_path / "config.toml"
    config_path.write_text(dump_config_toml(config), encoding="utf-8")
    assert load_config(config_path).model_dump() == config.model_dump()


# ---------- 保存：掩码保留 / 校验不落盘 ----------


async def test_post_mask_placeholder_preserves_old_key(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    config = await get_config(http_client, webui_stack.bridge_url)
    fallback = dict(config["fallback"])
    fallback["model"] = "MASKED-SAVE-MODEL"  # api_key 仍是 GET 回来的掩码占位
    status, data = await post_config(http_client, webui_stack.bridge_url, {"fallback": fallback})
    assert status == 200 and data["ok"] is True
    # 落盘文件里旧密钥原样保留、新 model 生效
    saved = webui_stack.config_path.read_text(encoding="utf-8")
    assert SECRET_KEY in saved
    assert "MASKED-SAVE-MODEL" in saved
    assert MASKED_KEY not in saved  # 掩码占位绝不落盘


async def test_post_validation_failure_does_not_write(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    base = webui_stack.bridge_url
    # 先正常保存一次，让 config.toml 存在且含真实密钥
    config = await get_config(http_client, base)
    fallback = dict(config["fallback"])
    fallback["model"] = "GOOD-MODEL"
    status, _ = await post_config(http_client, base, {"fallback": fallback})
    assert status == 200
    before = webui_stack.config_path.read_text(encoding="utf-8")

    # 非法提交：超时非数字 → 校验失败 → 400，磁盘不动
    bad = dict(config["fallback"])
    bad["timeout_seconds"] = "not-a-number"
    status, data = await post_config(http_client, base, {"fallback": bad})
    assert status == 400
    assert data["error"] == "配置校验失败，未保存"
    # 校验错误瘦身：不回显提交的密钥
    assert SECRET_KEY not in json.dumps(data)
    assert webui_stack.config_path.read_text(encoding="utf-8") == before
    # 配置在内存中也不被污染
    after = await get_config(http_client, base)
    assert after["fallback"]["model"] == "GOOD-MODEL"
    assert after["fallback"]["api_key"] == MASKED_KEY


# ---------- 保存热生效（保存后路由即时变化，不重启） ----------


async def test_save_hot_applies_model_to_routing(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    base = webui_stack.bridge_url
    # intern-decision-* 模型 + 无解析器识别 → 直通回退且模型名改写为 fallback.model
    body = {"model": "intern-decision-4b", "messages": [{"role": "user", "content": "你好"}]}

    # 保存前：直通回退用旧配置里的回退模型
    async with http_client.post(base + "/v1/chat/completions", json=body) as response:
        assert response.status == 200
    assert webui_stack.fallback_calls[-1]["model"] == "GLM-TEST"

    # 保存新 model（api_key 用掩码占位）
    config = await get_config(http_client, base)
    fallback = dict(config["fallback"])
    fallback["model"] = "HOT-RELOADED"
    status, _ = await post_config(http_client, base, {"fallback": fallback})
    assert status == 200

    # 保存后：无需重启，后续请求立即用新 model
    async with http_client.post(base + "/v1/chat/completions", json=body) as response:
        assert response.status == 200
    assert webui_stack.fallback_calls[-1]["model"] == "HOT-RELOADED"


async def test_save_hot_disables_parser(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    base = webui_stack.bridge_url
    # 保存前：exprsel 模板请求被识别并翻译到 SystemOne
    async with http_client.post(
        base + "/v1/chat/completions", json=exprsel_request("exprsel_group_a.json")
    ) as response:
        assert response.status == 200

    # 关掉 expression_selector 解析器并保存
    config = await get_config(http_client, base)
    parsers = dict(config["task_parsers"])
    parsers["expression_selector"] = False
    status, _ = await post_config(http_client, base, {"task_parsers": parsers})
    assert status == 200

    # 保存后：同样的请求不再识别，直接直通回退（SystemOne 零调用）
    config = await get_config(http_client, base)
    assert config["task_parsers"]["expression_selector"] is False
    async with http_client.post(
        base + "/v1/chat/completions", json=exprsel_request("exprsel_group_a.json")
    ) as response:
        assert response.status == 200
        data = await response.json()
    assert data["choices"][0]["message"]["content"] == "fallback-ok"


# ---------- 连通性测试端点（打真实 mock 上游） ----------


async def test_test_endpoint_systemone_ok(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    async with http_client.post(webui_stack.bridge_url + "/api/test/systemone") as response:
        assert response.status == 200
        data = await response.json()
    assert data["ok"] is True
    assert data["category"] == "ok"
    assert data["status"] == 200


async def test_test_endpoint_fallback_ok(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    async with http_client.post(webui_stack.bridge_url + "/api/test/fallback") as response:
        assert response.status == 200
        data = await response.json()
    assert data["ok"] is True
    assert data["category"] == "ok"
    assert "可用" in data["detail"]
    # 测试探测只发 max_tokens=1 的最小请求
    assert webui_stack.fallback_calls[-1]["messages"] == [{"role": "user", "content": "ping"}]


async def test_test_endpoint_override_unreachable(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    # 覆盖 base_url 指向不可达端口 → 分类为 unreachable（不沿用当前配置）
    async with http_client.post(
        webui_stack.bridge_url + "/api/test/systemone",
        json={"base_url": "http://127.0.0.1:9"},
    ) as response:
        assert response.status == 200
        data = await response.json()
    assert data["ok"] is False
    assert data["category"] == "unreachable"


# ---------- 运行状态（northbound 打点接入） ----------


async def test_status_counts_routes_and_errors(
    webui_stack: WebuiStack, http_client: ClientSession
) -> None:
    base = webui_stack.bridge_url
    async with http_client.post(
        base + "/v1/chat/completions",
        json={"model": "GLM-PLAIN", "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        assert response.status == 200
    # 锚点命中但候选为空 → 解析失败直通，同时进最近错误
    body = exprsel_request("exprsel_group_a.json")
    body["messages"][0]["content"] = "请从下面候选中选择。候选表达方式：\n（本次为空）"
    async with http_client.post(base + "/v1/chat/completions", json=body) as response:
        assert response.status == 200
    async with http_client.get(base + "/api/status") as response:
        assert response.status == 200
        data = await response.json()
    assert data["total_requests"] == 2
    assert data["fallback_hits"] == 2
    assert data["systemone_hits"] == 0
    assert data["routes"].get("passthrough.direct") == 1
    assert data["routes"].get("passthrough.parse_error") == 1
    assert data["recent_errors"] and data["recent_errors"][-1]["kind"] == "parse_error"
    assert data["version"]
    assert data["server"]


# ---------- 首启 bootstrap 与序列化单元 ----------


def test_bootstrap_config_file_from_example(tmp_path: Path) -> None:
    example = tmp_path / "config.toml.example"
    example.write_text('[server]\nhost = "127.0.0.1"\n', encoding="utf-8")
    config_path = tmp_path / "config.toml"
    assert bootstrap_config_file(config_path, example) is True  # 首次生成
    assert config_path.read_bytes() == example.read_bytes()  # 原样拷贝（保留注释）
    assert bootstrap_config_file(config_path, example) is False  # 已存在不再生成


def test_bootstrap_config_file_missing_example(tmp_path: Path) -> None:
    # 模板也不存在 → 不生成，保持「缺省全默认」的旧行为
    assert bootstrap_config_file(tmp_path / "config.toml", tmp_path / "nope.example") is False
    assert not (tmp_path / "config.toml").exists()
