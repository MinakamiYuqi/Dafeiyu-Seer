from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

try:  # AstrBot >= 4.29：路径工具统一在 astrbot_path
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path
except ImportError:  # 旧版本（上游 main 分支的写法）
    from astrbot.core.utils.astrbot_plugin_path import get_astrbot_plugin_path


class ConfigNode:
    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] missing field: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)
            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"field {key} expects dict, got {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]
            return value
        if key in self.__dict__:
            return self.__dict__[key]
        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() must be called on root config node"
            )
        self._data.save_config()


class PluginConfig(ConfigNode):
    seerapi_sync_url: str
    seerapi_fingerprint_url: str
    seerapi_sync_interval_minutes: int
    alias_sync_url: str
    alias_fingerprint_url: str
    alias_sync_interval_minutes: int
    render_mode: str
    image_format: str
    jpeg_quality: int
    # 旧配置文件里可能没有这几项，声明为可选，缺失时由 main.py 回退到默认值
    fuzzy_command: bool | None
    bare_name_search: bool | None
    auto_dispatch: bool | None
    ai_fallback: str | None
    ai_fallback_provider_id: str | None
    ai_fallback_max_length: int | None
    llm_awareness: bool | None
    llm_capability_prompt: str | None
    llm_denial_hint: str | None
    pet_config_uploaders: str | None
    pet_aliases: str | None
    render_cache: bool | None
    render_cache_max_size_mb: int | None

    _plugin_name: str = "astrbot_plugin_seer_info"

    def __init__(self, cfg: AstrBotConfig, context):
        super().__init__(cfg)
        self.context = context
        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
