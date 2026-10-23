"""配置加载：config.toml（可选，缺省全默认）+ pydantic 校验 + 保存/首启生成。"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("systemone_bridge.config")


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18500


class UpstreamSystemOneConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8100"
    # 决策端点路径：官方开源 serving 为 /v1/decisions；OpenXLab 托管网关为 /v1/systemone
    decisions_path: str = "/v1/decisions"
    api_key: str = ""
    # 注入请求体顶层的 model 字段（网关校验必需）；留空则不注入，保持官方 serving 兼容
    model: str = ""
    timeout_seconds: float = 10.0


class FallbackConfig(BaseModel):
    base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4"
    api_key: str = ""
    model: str = "GLM-5.3-Flash"
    timeout_seconds: float = 120.0


class TaskParsersConfig(BaseModel):
    expression_selector: bool = True
    df_check_planner: bool = True


class TranslationConfig(BaseModel):
    expression_min_probability: float = Field(default=0.05, ge=0.0, le=1.0)
    expression_max_selected: int = Field(default=5, ge=0, le=62)


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dir: str = "data/logs"


class BridgeConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    upstream_systemone: UpstreamSystemOneConfig = UpstreamSystemOneConfig()
    fallback: FallbackConfig = FallbackConfig()
    task_parsers: TaskParsersConfig = TaskParsersConfig()
    translation: TranslationConfig = TranslationConfig()
    logging: LoggingConfig = LoggingConfig()


def load_config(path: Path | None) -> BridgeConfig:
    """从 TOML 加载配置；文件不存在时用全默认值（便于先跑通）。"""

    if path is None or not path.is_file():
        return BridgeConfig()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    return BridgeConfig.model_validate(raw)


def bootstrap_config_file(config_path: Path, example_path: Path) -> bool:
    """首次启动 bootstrap：config.toml 不存在时从模板原样生成（保留注释）。

    返回是否新生成了文件；模板也不存在时不生成（保持"缺省全默认"的旧行为）。
    """

    if config_path.is_file() or not example_path.is_file():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(example_path.read_bytes())
    logger.info("首次启动：已从 %s 生成 %s，可开箱使用（密钥可在 WebUI 填写）", example_path.name, config_path.name)
    return True


# ---------- TOML 序列化（WebUI 保存用；与 load_config 互为逆操作） ----------

# 各节注释模板：与 config.toml.example 的口径保持一致（保存后文件仍可读）
_TOML_COMMENTS: dict[str, tuple[str, ...]] = {
    "server": ("监听地址/端口；改端口需重启服务生效",),
    "upstream_systemone": (
        "南向 SystemOne（Intern-Decision 决策端点）上游。",
        "不可达/超时/报错时自动直通回退；api_key 留空则不发 Authorization。",
        "decisions_path：官方开源 serving 为 /v1/decisions，OpenXLab 托管网关为 /v1/systemone。",
        "model：注入请求体顶层 model 字段（网关校验必需）；留空则不注入，保持官方兼容。",
    ),
    "fallback": (
        "回退上游（OpenAI 兼容）：任何情况下桥不比直接打这个上游更差。",
        "北向 model 指向 intern-decision-* 但被迫回退时改写为该 model。",
    ),
    "task_parsers": ("任务解析器开关：关闭后对应任务直接走直通回退",),
    "translation": ("exprsel 概率映射：最低入选概率与最多入选条数",),
    "logging": ("日志级别与目录",),
}

# 节内键的输出顺序（新键追加在末尾，不致错位）
_TOML_KEY_ORDER: dict[str, tuple[str, ...]] = {
    "server": ("host", "port"),
    "upstream_systemone": ("base_url", "decisions_path", "api_key", "model", "timeout_seconds"),
    "fallback": ("base_url", "api_key", "model", "timeout_seconds"),
    "task_parsers": ("expression_selector", "df_check_planner"),
    "translation": ("expression_min_probability", "expression_max_selected"),
    "logging": ("level", "dir"),
}


def _dump_toml_value(value: Any) -> str:
    """把标量配置值序列化为 TOML 字面量（bool/数字/字符串）。"""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump_config_toml(config: BridgeConfig) -> str:
    """把配置序列化为带注释的 TOML 文本（WebUI 保存时原子写盘的内容）。"""

    lines: list[str] = [
        "# systemone-bridge 配置文件（由 WebUI 保存生成）。",
        "# 真实 config.toml 不入库（见 .gitignore）；字段说明见 README 或 config.toml.example。",
        "",
    ]
    data = config.model_dump()
    for section, keys in _TOML_KEY_ORDER.items():
        values = data.get(section) or {}
        comments = _TOML_COMMENTS.get(section, ())
        lines.append(f"# {comments[0]}" if comments else "")
        for comment in comments[1:]:
            lines.append(f"# {comment}")
        lines.append(f"[{section}]")
        for key in keys:
            if key in values:
                lines.append(f"{key} = {_dump_toml_value(values[key])}")
        for key, value in values.items():
            if key not in keys:
                lines.append(f"{key} = {_dump_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
