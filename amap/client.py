"""高德地图开放平台 Web 服务 API 的异步客户端。

设计要点：
- 只依赖 aiohttp（AstrBot 环境自带），不引第三方 SDK。
- 所有接口统一走 `_request()`：拼 key、可选数字签名(sig)、超时、重试、
  错误码归一化，最后把高德那套 `status/info/infocode` 变成异常或数据。
- 高德返回的是 GCJ-02 坐标系；调用方需要自己保证传进去的是 GCJ-02
  （本插件的惯例：手机上报 WGS-84 → coord.py 转 GCJ-02 → 再进这里）。
- 未配置 key 时抛 AmapError("NO_KEY")，上层会转成给 agent 看的友好提示。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from astrbot.api import logger

try:  # aiohttp 在 AstrBot 运行时必然存在；这里兜底是为了离线单测能给出清晰报错
    import aiohttp
except Exception as exc:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_ERR = exc
else:
    _AIOHTTP_ERR = None


# 常见信息码 → 人话。没列到的直接回显 info/infocode。
INFO_HINTS: dict[str, str] = {
    "10000": "请求成功",
    "10001": "key 不正确或已过期",
    "10002": "没有权限使用相应的服务，请检查 key 绑定的服务类型（本插件需要「Web服务」类型的 key）",
    "10003": "日访问量已超限",
    "10004": "单位时间内访问过于频繁（QPS 超限）",
    "10005": "IP 白名单出错，请检查高德控制台的 IP 白名单设置",
    "10006": "绑定域名无效",
    "10007": "数字签名未通过验证，请检查 amap_sig_secret",
    "10008": "MD5 加密出错",
    "10009": "请求 key 与绑定平台不符（本插件要用「Web服务」key，不是 Web端/Android 的 key）",
    "10012": "权限不足，服务请求被拒绝",
    "10013": "key 被删除或不存在",
    "10014": "key 过期",
    "10016": "当前服务已下线",
    "10017": "当前服务不存在",
    "10019": "使用量超出配额",
    "10020": "高德服务正在维护",
    "10021": "高德服务已下线",
    "10010": "单个 IP 的访问量超限（被封停后需要提工单才能恢复，注意别压测）",
    "10011": "账号已欠费",
    "10015": "账号未完成认证或无该服务权限",
    "20000": "请求参数非法，请检查必填参数",
    "20001": "缺少必填参数",
    "20002": "请求协议非法",
    "20003": "请求方式错误（GET/POST 用错）",
    "20011": "距离测量参数错误",
    "20800": "规划点（起终点）不在中国境内",
    "20801": "规划点（起终点）太远，超出服务范围",
    "20802": "规划点（起终点）附近没有可用的道路",
    "20803": "起点与终点相同",
    "30001": "无结果（换关键词或放宽条件再试）",
    "30003": "查询不到该地址",
    "30004": "没有找到 POI",
    "30005": "地址解析失败",
    "20804": "路径规划失败",
    "40000": "请求参数非法",
    "40001": "请求参数非法（参数值超出范围）",
    "40002": "缺少必填参数",
    "40003": "请求参数非法（参数类型错误）",
    "40004": "请求参数非法（参数格式错误）",
    "40005": "请求参数非法（参数值非法）",
    "50000": "高德服务端内部错误",
}


def _clean(params: dict[str, Any]) -> dict[str, str]:
    """把参数里的 None / 空串 / 空列表丢掉，其余转成字符串。"""
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            joined = "|".join(str(x) for x in value if str(x).strip())
            if joined:
                out[key] = joined
            continue
        text = str(value)
        if text.strip() == "":
            continue
        out[key] = text
    return out


class AmapError(Exception):
    """高德调用失败（含本地 key 缺失、网络错误）。"""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.path = str(path)

    def human(self) -> str:
        text = "高德接口调用失败"
        if self.path:
            text += "（" + self.path + "）"
        return text + "：" + self.message + " [code=" + self.code + "]"


class _TTLCache:
    """极简 TTL 缓存。

    高德的配额是**按月**算的，而且搜索类（POI/输入提示）和天气各只有 5000 次/月，
    所以同一句话反复查（agent 常见行为）必须挡住。默认缓存 2 分钟。
    """

    def __init__(self, ttl: float, maxsize: int = 512) -> None:
        self.ttl = float(ttl)
        self.maxsize = int(maxsize)
        self._data: dict[str, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        if self.ttl <= 0:
            return None
        row = self._data.get(key)
        if row is None:
            self.misses += 1
            return None
        expire_at, value = row
        if time.time() >= expire_at:
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value) -> None:
        if self.ttl <= 0:
            return
        if len(self._data) >= self.maxsize:
            # 简单粗暴地淘汰最旧的一批，够用了
            for old_key in list(self._data.keys())[: max(1, self.maxsize // 4)]:
                self._data.pop(old_key, None)
        self._data[key] = (time.time() + self.ttl, value)

    def clear(self) -> None:
        self._data.clear()



class AmapClient:
    """高德 Web 服务异步客户端。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._session: "aiohttp.ClientSession | None" = None
        self._session_lock = asyncio.Lock()
        self.stats = {"calls": 0, "errors": 0, "last_path": "", "last_error": ""}
        self.cache = _TTLCache(float(getattr(cfg, "cache_ttl", 0) or 0))

    # ------------------------------------------------------------------ 会话
    async def _get_session(self) -> "aiohttp.ClientSession":
        if _AIOHTTP_ERR is not None:  # pragma: no cover
            raise AmapError("NO_AIOHTTP", "缺少 aiohttp 依赖：" + repr(_AIOHTTP_ERR))
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            timeout = aiohttp.ClientTimeout(total=int(self.cfg.request_timeout))
            self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as exc:  # pragma: no cover
                logger.warning("amap: 关闭 HTTP 会话出错: " + repr(exc))
        self._session = None

    # ------------------------------------------------------------------ 签名
    def _sign(self, params: dict[str, str]) -> str:
        """高德数字签名：把参数按 key 升序拼成 k=v&k=v，再拼私钥，md5。

        注意用的是**未 urlencode 的原始值**拼接。
        """
        secret = str(self.cfg.amap_sig_secret or "").strip()
        if not secret:
            return ""
        query = "&".join(str(k) + "=" + str(v) for k, v in sorted(params.items()))
        return hashlib.md5((query + secret).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 请求
    async def _request(self, path: str, params: dict[str, Any], raw: bool = False):
        """发一次高德请求。

        raw=True 时直接返回 bytes（静态地图这类图片接口）。
        """
        if not self.cfg.has_key:
            raise AmapError(
                "NO_KEY",
                "还没有配置高德 Web 服务 key（在 AstrBot WebUI 的插件配置里填 amap_key）",
                path,
            )
        query = _clean(params)
        cache_key = path + "|" + "&".join(str(k) + "=" + str(v) for k, v in sorted(query.items()))
        if not raw:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit
        query["key"] = str(self.cfg.amap_key).strip()
        if str(self.cfg.amap_sig_secret or "").strip():
            # 签名算法：除 sig 外全部参数按名升序拼 k=v&...&key=xxx，直接接私钥做 MD5
            query["sig"] = self._sign(query)
        url = str(self.cfg.api_base).rstrip("/") + path
        attempts = max(1, int(self.cfg.retry_times) + 1)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            self.stats["calls"] += 1
            self.stats["last_path"] = path
            try:
                session = await self._get_session()
                async with session.get(url, params=query) as resp:
                    body = await resp.read()
                    if resp.status != 200:
                        raise AmapError(
                            "HTTP_" + str(resp.status),
                            "HTTP " + str(resp.status) + " " + body[:200].decode("utf-8", "replace"),
                            path,
                        )
                    if raw:
                        # 静态地图这类接口出错时返回的是 JSON 文本而不是图片，必须判别，
                        # 否则会把一段报错 JSON 当成 PNG 存下来。
                        if body[:1] == b"{":
                            try:
                                err = json.loads(body.decode("utf-8", "replace"))
                            except Exception:
                                err = {}
                            info = str(err.get("info", "")) if isinstance(err, dict) else ""
                            infocode = str(err.get("infocode", "")) if isinstance(err, dict) else ""
                            hint = INFO_HINTS.get(infocode, "")
                            message = info or "接口返回了错误文本"
                            if hint and hint != info:
                                message += "（" + hint + "）"
                            raise AmapError(infocode or "BAD_IMAGE", message, path)
                        return body
                    try:
                        data = json.loads(body.decode("utf-8", "replace"))
                    except Exception as exc:
                        raise AmapError("BAD_JSON", "返回不是合法 JSON: " + repr(exc), path)
                    if not isinstance(data, dict):
                        raise AmapError("BAD_JSON", "返回结构不是对象", path)
                    status = str(data.get("status", ""))
                    if status != "1":
                        info = str(data.get("info", ""))
                        infocode = str(data.get("infocode", ""))
                        hint = INFO_HINTS.get(infocode, "")
                        message = info or "未知错误"
                        if hint and hint != info:
                            message += "（" + hint + "）"
                        raise AmapError(infocode or "UNKNOWN", message, path)
                    self.cache.set(cache_key, data)
                    return data
            except AmapError as exc:
                last_exc = exc
                # key / 参数类错误重试没意义，直接抛
                if exc.code.startswith(("1", "2", "4", "NO_", "BAD_")):
                    raise
                if attempt + 1 >= attempts:
                    raise
            except asyncio.TimeoutError:
                last_exc = AmapError("TIMEOUT", "请求超过 " + str(self.cfg.request_timeout) + " 秒未返回", path)
                if attempt + 1 >= attempts:
                    raise last_exc
            except Exception as exc:
                last_exc = AmapError("NETWORK", "网络异常: " + repr(exc), path)
                if attempt + 1 >= attempts:
                    raise last_exc
            await asyncio.sleep(0.4 * (attempt + 1))

        self.stats["errors"] += 1
        self.stats["last_error"] = str(last_exc)
        raise last_exc if last_exc else AmapError("UNKNOWN", "未知错误", path)

    # ------------------------------------------------------------------ 地理编码
    async def geocode(self, address: str, city: str = "") -> dict:
        """结构化地址 → 坐标（/geocode/geo）。"""
        return await self._request(
            "/v3/geocode/geo",
            {"address": address, "city": city, "output": "JSON"},
        )

    async def regeo(
        self,
        lng: float,
        lat: float,
        radius: int = 1000,
        extensions: str = "all",
        poitype: str = "",
        roadlevel: int = 0,
    ) -> dict:
        """坐标 → 结构化地址（/geocode/regeo）。extensions=all 才会带 POI/AOI/道路。"""
        return await self._request(
            "/v3/geocode/regeo",
            {
                "location": str(lng) + "," + str(lat),
                "radius": radius,
                "extensions": extensions,
                "poitype": poitype,
                "roadlevel": roadlevel,
                "output": "JSON",
            },
        )

    # ------------------------------------------------------------------ 天气
    async def weather(self, city: str, extensions: str = "all") -> dict:
        """天气（/weather/weatherInfo）。city 可以是 adcode 或城市名；all 同时返回实况与预报。"""
        return await self._request(
            "/v3/weather/weatherInfo",
            {"city": city, "extensions": extensions, "output": "JSON"},
        )

    # ------------------------------------------------------------------ IP
    async def ip_locate(self, ip: str = "") -> dict:
        """IP 定位（/ip）。ip 留空则用请求来源 IP。"""
        return await self._request("/v3/ip", {"ip": ip, "output": "JSON"})

    # ------------------------------------------------------------------ POI
    async def poi_text(
        self,
        keywords: str,
        city: str = "",
        citylimit: bool = False,
        page: int = 1,
        offset: int = 10,
        types: str = "",
    ) -> dict:
        """关键字搜索 POI（/place/text）。"""
        return await self._request(
            "/v3/place/text",
            {
                "keywords": keywords,
                "city": city,
                "citylimit": "true" if citylimit else "",
                "page": max(1, int(page)),
                "offset": max(1, min(25, int(offset))),
                "types": types,
                "extensions": "all",
                "output": "JSON",
            },
        )

    async def poi_around(
        self,
        lng: float,
        lat: float,
        keywords: str = "",
        radius: int = 1000,
        page: int = 1,
        offset: int = 10,
        types: str = "",
        sortrule: str = "distance",
    ) -> dict:
        """周边搜索 POI（/place/around）。"""
        return await self._request(
            "/v3/place/around",
            {
                "location": str(lng) + "," + str(lat),
                "keywords": keywords,
                "radius": max(1, min(50000, int(radius))),
                "page": max(1, int(page)),
                "offset": max(1, min(25, int(offset))),
                "types": types,
                "sortrule": sortrule,
                "extensions": "all",
                "output": "JSON",
            },
        )

    async def poi_detail(self, poi_id: str) -> dict:
        """POI 详情（/place/detail）。"""
        return await self._request("/v3/place/detail", {"id": poi_id, "output": "JSON"})

    async def inputtips(self, keywords: str, city: str = "", location: str = "", datatype: str = "all") -> dict:
        """输入提示（/assistant/inputtips）。"""
        return await self._request(
            "/v3/assistant/inputtips",
            {"keywords": keywords, "city": city, "location": location, "datatype": datatype, "output": "JSON"},
        )

    # ------------------------------------------------------------------ 路径规划
    async def route(
        self,
        mode: str,
        origin: str,
        destination: str,
        strategy: int | None = None,
        city: str = "",
        cityd: str = "",
        waypoints: str = "",
        extensions: str = "all",
    ) -> dict:
        """路径规划。mode: driving / walking / bicycling / transit。

        origin / destination 形如 "116.48,39.99"（GCJ-02）。
        """
        mode = str(mode or "driving").strip().lower()
        common = {
            "origin": origin,
            "destination": destination,
            "extensions": extensions,
            "output": "JSON",
        }
        if mode in ("driving", "car", "驾车"):
            params = dict(common)
            if strategy is not None:
                params["strategy"] = int(strategy)
            if waypoints:
                params["waypoints"] = waypoints
            return await self._request("/v3/direction/driving", params)
        if mode in ("walking", "walk", "步行"):
            return await self._request("/v3/direction/walking", dict(common))
        if mode in ("bicycling", "bike", "骑行"):
            return await self._request("/v3/direction/bicycling", dict(common))
        if mode in ("transit", "bus", "公交"):
            params = dict(common)
            params["city"] = city
            if cityd:
                params["cityd"] = cityd
            if strategy is not None:
                params["strategy"] = int(strategy)
            params["nightflag"] = 0
            return await self._request("/v3/direction/transit/integrated", params)
        raise AmapError("BAD_MODE", "不支持的出行方式：" + mode + "（可用 driving/walking/bicycling/transit）")

    async def distance(self, origins: str, destination: str, dist_type: int = 1) -> dict:
        """距离测量（/distance）。origins 支持多个点，用 | 分隔；type=1 直线 / 0 驾车。"""
        return await self._request(
            "/v3/distance",
            {"origins": origins, "destination": destination, "type": int(dist_type), "output": "JSON"},
        )

    # ------------------------------------------------------------------ 行政区划
    async def district(self, keywords: str = "", subdistrict: int = 1, extensions: str = "base") -> dict:
        """行政区划查询（/config/district）。"""
        return await self._request(
            "/v3/config/district",
            {"keywords": keywords, "subdistrict": int(subdistrict), "extensions": extensions, "output": "JSON"},
        )

    # ------------------------------------------------------------------ 坐标转换（官方）
    async def coordinate_convert(self, locations: str, coordsys: str = "gps") -> dict:
        """官方坐标转换（/assistant/coordinate/convert）。

        coordsys: gps(WGS84) / map(GCJ02) / baidu(BD09)，转出的都是 GCJ-02。
        locations 形如 "116.48,39.99;116.49,39.98"。
        """
        return await self._request(
            "/v3/assistant/coordinate/convert",
            {"locations": locations, "coordsys": coordsys, "output": "JSON"},
        )

    # ------------------------------------------------------------------ 静态地图
    async def static_map(
        self,
        location: str,
        zoom: int = 14,
        size: str = "750*500",
        scale: int = 2,
        markers: str = "",
        paths: str = "",
        traffic: int = 0,
        labels: str = "",
    ) -> bytes:
        """静态地图（/staticmap），返回图片二进制。"""
        params: dict[str, Any] = {
            "location": location,
            "zoom": max(1, min(17, int(zoom))),
            "size": size,
            "scale": int(scale),
            "traffic": int(traffic),
        }
        if markers:
            params["markers"] = markers
        if paths:
            params["paths"] = paths
        if labels:
            params["labels"] = labels
        return await self._request("/v3/staticmap", params, raw=True)
