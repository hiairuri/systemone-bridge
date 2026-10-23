"""配置加载：config.toml（可选，缺省全默认）+ pydantic 校验。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18500


class UpstreamSystemOneConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8100"
    api_key: str = ""
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
