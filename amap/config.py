"""astrbot_plugin_amap 配置解析。

AstrBot 会把 WebUI 里填的配置以 dict 形式传给插件构造函数，这里统一做
类型收敛与默认值兜底，避免后面到处写 getattr / try。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y", "开"):
        return True
    if text in ("0", "false", "no", "off", "n", "关"):
        return False
    return default


def _as_int(value: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        num = int(float(value))
    except Exception:
        return default
    if low is not None:
        num = max(low, num)
    if high is not None:
        num = min(high, num)
    return num


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


@dataclass
class AmapConfig:
    """插件全部可调参数。"""

    # --- 本机 HTTP 服务（手机 App 轮询用） ---
    enabled: bool = True
    listen_host: str = "0.0.0.0"
    listen_port: int = 6310
    auth_token: str = ""

    # --- 高德开放平台 ---
    amap_key: str = ""
    amap_sig_secret: str = ""
    api_base: str = "https://restapi.amap.com"
    request_timeout: int = 10
    retry_times: int = 1
    cache_ttl: int = 120

    # --- 坐标 ---
    auto_convert: bool = True
    convert_host: str = "local"

    # --- 定位拉取 ---
    locate_timeout: int = 45
    locate_max_age: int = 15
    high_accuracy: bool = True
    default_device: str = ""
    locate_fallback_city: str = ""

    # --- 设备与存储 ---
    poll_interval: int = 15
    device_offline_sec: int = 90
    history_limit: int = 200
    persist: bool = True

    # --- 输出裁剪 ---
    max_text_chars: int = 6000
    poi_page_size: int = 8

    # --- 静态地图 ---
    static_map_zoom: int = 14
    static_map_size: str = "750*500"
    static_map_dir: str = ""

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "AmapConfig":
        data = raw if isinstance(raw, dict) else {}
        cfg = cls(
            enabled=_as_bool(data.get("enabled"), True),
            listen_host=_as_str(data.get("listen_host"), "0.0.0.0"),
            listen_port=_as_int(data.get("listen_port"), 6310, 1, 65535),
            auth_token=_as_str(data.get("auth_token"), ""),
            amap_key=_as_str(data.get("amap_key"), ""),
            amap_sig_secret=_as_str(data.get("amap_sig_secret"), ""),
            api_base=_as_str(data.get("api_base"), "https://restapi.amap.com").rstrip("/"),
            request_timeout=_as_int(data.get("request_timeout"), 10, 1, 120),
            retry_times=_as_int(data.get("retry_times"), 1, 0, 5),
            cache_ttl=_as_int(data.get("cache_ttl"), 120, 0, 86400),
            auto_convert=_as_bool(data.get("auto_convert"), True),
            convert_host=_as_str(data.get("convert_host"), "local").lower(),
            locate_timeout=_as_int(data.get("locate_timeout"), 45, 5, 300),
            locate_max_age=_as_int(data.get("locate_max_age"), 15, 0, 3600),
            high_accuracy=_as_bool(data.get("high_accuracy"), True),
            default_device=_as_str(data.get("default_device"), ""),
            locate_fallback_city=_as_str(data.get("locate_fallback_city"), ""),
            poll_interval=_as_int(data.get("poll_interval"), 15, 3, 600),
            device_offline_sec=_as_int(data.get("device_offline_sec"), 90, 10, 86400),
            history_limit=_as_int(data.get("history_limit"), 200, 0, 20000),
            persist=_as_bool(data.get("persist"), True),
            max_text_chars=_as_int(data.get("max_text_chars"), 6000, 200, 200000),
            poi_page_size=_as_int(data.get("poi_page_size"), 8, 1, 25),
            static_map_zoom=_as_int(data.get("static_map_zoom"), 14, 1, 17),
            static_map_size=_as_str(data.get("static_map_size"), "750*500"),
            static_map_dir=_as_str(data.get("static_map_dir"), ""),
        )
        known = set(cfg.__dataclass_fields__.keys())
        cfg.extra = {k: v for k, v in data.items() if k not in known}
        return cfg

    # ------------------------------------------------------------------
    @property
    def has_key(self) -> bool:
        """是否已经填了高德 key。"""
        return bool(self.amap_key.strip())

    def describe(self) -> str:
        """给人看的配置摘要（绝不回显 key 全文）。"""
        if not self.has_key:
            key_state = "未配置（所有高德接口都会返回提示）"
        else:
            key = self.amap_key.strip()
            key_state = key[:4] + "****" + key[-4:] if len(key) > 10 else "****"
        return (
            "监听: http://" + self.listen_host + ":" + str(self.listen_port) + "\n"
            + "高德 key: " + key_state + "\n"
            + "数字签名: " + ("已启用" if self.amap_sig_secret.strip() else "未启用") + "\n"
            + "API 基址: " + self.api_base + "\n"
            + "坐标自动转换(WGS84→GCJ02): " + ("开" if self.auto_convert else "关")
            + "（转换方式: " + self.convert_host + "）\n"
            + "定位等待: " + str(self.locate_timeout) + "s"
            + "  缓存可用年龄: " + str(self.locate_max_age) + "s"
            + "  高精度: " + ("是" if self.high_accuracy else "否") + "\n"
            + "手机建议轮询间隔: " + str(self.poll_interval) + "s"
            + "  设备离线阈值: " + str(self.device_offline_sec) + "s\n"
            + "默认设备: " + (self.default_device or "(未设置)")
        )
