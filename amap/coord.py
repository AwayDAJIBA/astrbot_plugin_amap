"""坐标系统转换。

- WGS-84：GPS 原始坐标，安卓 LocationManager / 手机上报给我们的就是这个。
- GCJ-02：国测局火星坐标，高德/腾讯等国内服务的通用坐标。
- BD-09：百度坐标（本插件不直接用，但提供 GCJ-02 ↔ BD-09 便于对照）。

高德所有 Web 服务接口都吃 GCJ-02（`/assistant/coordinate/convert` 除外）。
手机上报的是 WGS-84，所以调高德之前必须先转，否则会有几百米级偏移。

这里的 `wgs84_to_gcj02` 用公开的经典近似算法（国内民用精度足够，误差约 1~2 米），
不消耗高德配额、不依赖网络。若想用高德官方转换，把配置 convert_host 设成 api 即可。
"""
from __future__ import annotations

import math

__all__ = [
    "out_of_china",
    "wgs84_to_gcj02",
    "gcj02_to_wgs84",
    "gcj02_to_bd09",
    "bd09_to_gcj02",
    "convert_point",
    "distance_meters",
]

_A = 6378245.0  # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323  # 偏心率平方
_X_PI = math.pi * 3000.0 / 180.0


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _valid(lng: float, lat: float) -> bool:
    return -180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0


def out_of_china(lng: float, lat: float) -> bool:
    """粗略判断是否在中国境外（境外不做偏移）。"""
    if not _valid(lng, lat):
        return True
    if lng < 72.004 or lng > 137.8347:
        return True
    if lat < 0.8293 or lat > 55.8271:
        return True
    return False


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（境外坐标原样返回）。"""
    lng = float(lng)
    lat = float(lat)
    if not _valid(lng, lat) or out_of_china(lng, lat):
        return lng, lat
    d_lat = _transform_lat(lng - 105.0, lat - 35.0)
    d_lng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lng = (d_lng * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lng + d_lng, lat + d_lat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → WGS-84（反向近似：正向加偏移后迭代收敛）。"""
    lng = float(lng)
    lat = float(lat)
    if not _valid(lng, lat) or out_of_china(lng, lat):
        return lng, lat
    # 迭代 3 次即可收敛到厘米级
    cur_lng, cur_lat = lng, lat
    for _ in range(3):
        g_lng, g_lat = wgs84_to_gcj02(cur_lng, cur_lat)
        cur_lng += lng - g_lng
        cur_lat += lat - g_lat
    return cur_lng, cur_lat


def gcj02_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → BD-09。"""
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """BD-09 → GCJ-02。"""
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def convert_point(lng: float, lat: float, src: str, dst: str) -> tuple[float, float]:
    """在 WGS-84 / GCJ-02 / BD-09 之间转换。

    src/dst 取值（大小写不敏感）: wgs84 / gcj02 / bd09。
    相同坐标系直接原样返回。
    """
    s = str(src or "").strip().lower().replace("-", "").replace("_", "")
    d = str(dst or "").strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "wgs84": "wgs84", "wgs": "wgs84", "gps": "wgs84", "epsg4326": "wgs84",
        "gcj02": "gcj02", "gcj": "gcj02", "amap": "gcj02", "gaode": "gcj02", "mars": "gcj02",
        "bd09": "bd09", "bd": "bd09", "baidu": "bd09",
    }
    s = aliases.get(s, "gcj02")
    d = aliases.get(d, "gcj02")
    if s == d:
        return float(lng), float(lat)
    # 先统一到 gcj02
    if s == "wgs84":
        glng, glat = wgs84_to_gcj02(lng, lat)
    elif s == "bd09":
        glng, glat = bd09_to_gcj02(lng, lat)
    else:
        glng, glat = float(lng), float(lat)
    if d == "gcj02":
        return glng, glat
    if d == "wgs84":
        return gcj02_to_wgs84(glng, glat)
    return gcj02_to_bd09(glng, glat)


def distance_meters(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离（米），用于「离上次位置有多远」这类判断。"""
    rad = math.pi / 180.0
    r = 6371000.0
    d_lat = (float(lat2) - float(lat1)) * rad
    d_lng = (float(lng2) - float(lng1)) * rad
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(float(lat1) * rad) * math.cos(float(lat2) * rad) * math.sin(d_lng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))
