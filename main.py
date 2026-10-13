"""astrbot_plugin_amap：高德地图能力 + 手机按需定位（AstrBot 侧入口）。

手机 --HTTP(明文, 内网)--> 本插件 aiohttp 服务(默认 0.0.0.0:6310) --> 位置入库
agent 调 locate_phone 时才下发定位请求，手机取一次 GPS 立刻回传（不常驻、不耗电）。

给 agent 的工具（13 个）：
    设备与定位
        list_phone_devices()                            看哪些手机连过主机
        locate_phone(device_id, timeout, force)         按需拉取手机当前位置 + 中文地址
        where_is_phone(device_id, with_weather)         一步到位：位置 + 地址 + 当地天气
        phone_location_history(device_id, limit)        位置历史
    高德地图
        amap_geocode(address, city)                     地址 → 坐标
        amap_reverse_geocode(lng, lat)                  坐标 → 地址
        amap_weather(city, forecast)                    实况 + 预报
        amap_search_place(keywords, city, near, radius) 关键字 / 周边搜 POI
        amap_route(origin, destination, mode, city)     驾车/步行/骑行/公交 路径规划
        amap_measure_distance(origins, destination)     距离测量
        amap_ip_location(ip)                            IP 定位
        amap_district(keywords, subdistrict)            行政区划
        amap_static_map(center, zoom, markers)          静态地图（存成图片）
    amap_status()                                       配置与运行自检

调试命令：/amap_status、/amap_loc <设备>、/amap_where <设备>、/amap_map <地点> [zoom]
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
from astrbot.api.star import Context, Star, StarTools, register

from .amap import coord
from .amap import formatter as fmt
from .amap import netinfo
from .amap.client import AmapClient, AmapError
from .amap.config import AmapConfig
from .amap.locator import LocateManager
from .amap.server import AmapHttpServer
from .amap.store import LocationStore

PLUGIN_NAME = "astrbot_plugin_amap"

_KEY_MISSING_HINT = (
    "还没有配置高德 Web 服务 key。请到 AstrBot WebUI → 插件 → astrbot_plugin_amap 的配置里"
    "填 amap_key（高德开放平台 → 应用管理 → 创建应用 → 添加 Key → 服务平台选「Web服务」），"
    "填完重载插件即可。手机定位那部分不需要 key 也能用。"
)


def time_text(value) -> str:
    """unix 时间戳 → 本地时间字符串。"""
    if value is None:
        stamp = datetime.datetime.now()
    else:
        try:
            stamp = datetime.datetime.fromtimestamp(float(value))
        except Exception:
            return str(value)
    return stamp.strftime("%m-%d %H:%M:%S")


@register(PLUGIN_NAME, "Away", "高德地图能力 + 手机按需定位（agent 工具集）", "v1.0.0")
class AmapPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.cfg = AmapConfig.from_dict(config)
        self.data_dir = self._resolve_data_dir()
        self.store = LocationStore(self.data_dir, self.cfg)
        self.client = AmapClient(self.cfg)
        self.locator = LocateManager(self.cfg, self.store, self.client)
        self.server = (
            AmapHttpServer(self.cfg, self.store, self.locator, self.client) if self.cfg.enabled else None
        )
        logger.info(
            "amap: 配置已载入 enabled=" + str(self.cfg.enabled)
            + " port=" + str(self.cfg.listen_port)
            + " key=" + ("已配置" if self.cfg.has_key else "未配置")
        )

    def _resolve_data_dir(self) -> Path:
        try:
            path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:
            logger.warning("amap: 取 data 目录失败，回退到插件目录: " + repr(exc))
            path = Path(__file__).resolve().parent / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def map_dir(self) -> Path:
        path = Path(self.cfg.static_map_dir) if str(self.cfg.static_map_dir or "").strip() else (self.data_dir / "static_maps")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def initialize(self) -> None:
        if self.server is None:
            logger.warning("amap: enabled=false，HTTP 服务未启动（手机定位通道不可用）。")
            return
        try:
            await self.server.start()
        except Exception as exc:
            logger.error(
                "amap: HTTP 服务启动失败（端口 " + str(self.cfg.listen_port) + " 被占或非法？）：" + repr(exc)
            )
            self.server = None
            return
        logger.info(
            "amap: 手机 App 里「主机地址」可以填下面任意一个（出门在外请用组网地址）：\n"
            + netinfo.describe_addresses(int(self.cfg.listen_port))
        )

    async def terminate(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None
        try:
            self.store.flush()
        except Exception as exc:
            logger.warning("amap: 落盘状态失败: " + repr(exc))
        try:
            await self.client.close()
        except Exception as exc:
            logger.warning("amap: 关闭 HTTP 会话失败: " + repr(exc))

    # ==================================================================
    # 内部辅助
    # ==================================================================
    async def _call(self, awaitable) -> tuple[bool, object]:
        """统一包一层高德错误处理。返回 (成功?, 数据或错误文本)。"""
        try:
            return True, await awaitable
        except AmapError as exc:
            if exc.code == "NO_KEY":
                return False, _KEY_MISSING_HINT
            return False, exc.human()
        except Exception as exc:
            logger.warning("amap: 调用高德接口异常: " + repr(exc))
            return False, "调用高德接口时出错：" + repr(exc)

    async def _point(self, raw: str, city: str = "") -> tuple[float, float] | str:
        """把用户给的地点描述变成坐标。

        能直接解析成 "116.397,39.908" 的就当 GCJ-02 坐标用；
        否则拿去地理编码。返回 (lng, lat) 或错误文本。
        """
        body = str(raw or "").strip()
        if not body:
            return "地点不能为空，可以给「116.397,39.908」这样的坐标或一个地址。"
        point = fmt.parse_location(body)
        if point is not None:
            return point
        ok, data = await self._call(self.client.geocode(body, city=city))
        if not ok:
            return str(data)
        geocodes = data.get("geocodes") if isinstance(data, dict) else None  # type: ignore[union-attr]
        if not isinstance(geocodes, list) or not geocodes:
            return "没能把「" + body + "」解析成坐标（换个更完整的地址再试）。"
        first = geocodes[0] if isinstance(geocodes[0], dict) else {}
        resolved = fmt.parse_location(first.get("location"))
        if resolved is None:
            return "高德没返回可用坐标。"
        return resolved

    async def _to_adcode(self, city: str) -> str:
        """把「城市名 / adcode / 坐标」收敛成 adcode。

        高德天气接口的 city 参数要的是 **adcode**（官方示例用的是区县级 110101），
        直接丢中文城市名不保证能查出来，所以先拿行政区划接口翻译一次。
        行政区划属于基础 LBS 服务（配额比天气宽 30 倍），翻译这一下不心疼。
        """
        body = str(city or "").strip()
        if not body:
            return ""
        if body.isdigit() and 4 <= len(body) <= 6:
            return body
        point = fmt.parse_location(body)
        if point is not None:
            ok, data = await self._call(self.client.regeo(point[0], point[1], radius=1000, extensions="base"))
            if ok and isinstance(data, dict):
                comp = (data.get("regeocode") or {}).get("addressComponent")
                adcode = fmt.text(comp.get("adcode")) if isinstance(comp, dict) else ""
                if adcode:
                    return adcode
            return ""
        ok, data = await self._call(self.client.district(keywords=body, subdistrict=0))
        if ok and isinstance(data, dict):
            items = data.get("districts")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                adcode = fmt.text(items[0].get("adcode"))
                if adcode:
                    return adcode
        # 翻译不出来就原样传下去，让高德自己判断（有些城市名它其实认）
        return body

    def _key_missing(self) -> bool:
        return not self.cfg.has_key

    def _device_hint(self) -> str:
        rows = self.store.list_devices()
        if not rows:
            return "目前没有任何手机连过主机（确认手机 App 已启动、地址端口填对、同一个 WiFi）。"
        return "已知设备：" + ", ".join(
            str(r.get("device_id")) + ("(在线)" if r.get("online") else "(离线)") for r in rows
        )

    async def _location_text(self, device_id: str = "", timeout: int = 0, force: bool = False,
                             with_address: bool = True, with_weather: bool = False) -> str:
        """定位 + 逆地理（+天气）组合成一段给 agent 看的话。"""
        result = await self.locator.locate(
            device_id,
            reason="agent 请求定位",
            timeout=timeout,
            force=bool(force) or self.cfg.locate_max_age == 0,
        )
        if not result.get("ok"):
            return "定位失败：" + str(result.get("error"))

        dev_id = str(result.get("device_id"))
        rec = result.get("location") or {}
        from_text = "实时拉取" if result.get("from") == "fresh" else "最近缓存"
        lines = [
            "手机 " + dev_id + " 的位置（" + from_text + "，" + time_text(rec.get("server_ts")) + "）：",
            fmt.fmt_location_record(rec),
        ]
        lng = rec.get("gcj_lng")
        lat = rec.get("gcj_lat")
        if lng is None or lat is None:
            lng = rec.get("lng")
            lat = rec.get("lat")

        adcode = ""
        if with_address and lng is not None and lat is not None:
            ok, data = await self._call(self.client.regeo(float(lng), float(lat)))
            if ok:
                lines.append(fmt.fmt_regeo(data))  # type: ignore[arg-type]
                regeo = data.get("regeocode") if isinstance(data, dict) else None  # type: ignore[union-attr]
                comp = regeo.get("addressComponent") if isinstance(regeo, dict) else None
                if isinstance(comp, dict):
                    adcode = fmt.text(comp.get("adcode"))
                    # 顺手把地址信息回写，之后 list_devices / 天气推断就能直接用
                    self.store.update_location_meta(
                        dev_id,
                        {
                            "address": fmt.text(regeo.get("formatted_address")) if isinstance(regeo, dict) else "",
                            "adcode": adcode,
                            "city": fmt.text(comp.get("city")) or fmt.text(comp.get("province")),
                            "district": fmt.text(comp.get("district")),
                        },
                    )
            else:
                lines.append("（地址解析没成功：" + str(data) + "）")
        elif with_address and self._key_missing():
            lines.append("（" + _KEY_MISSING_HINT + "）")

        if with_weather:
            city = adcode or str(rec.get("city") or "") or self.cfg.locate_fallback_city
            if city:
                ok, data = await self._call(self.client.weather(city, extensions="all"))
                if ok:
                    wx = fmt.fmt_weather(data)  # type: ignore[arg-type]
                    lines.append("")
                    lines.append("当地天气：")
                    lines.append(wx)
                else:
                    lines.append("（天气查询没成功：" + str(data) + "）")
            else:
                lines.append("（拿不到 adcode，天气没查）")
        return "\n".join(lines)

    # ==================================================================
    # 工具 1~4：设备与定位
    # ==================================================================
    @llm_tool(name="list_phone_devices")
    async def list_phone_devices(self, event: AstrMessageEvent) -> str:
        """列出所有连过这台主机的手机设备，以及它们最近一次已知位置。

        想知道"手机在哪"之前先用它拿到 device_id。无参数。
        """
        rows = self.store.list_devices()
        if not rows:
            return (
                "目前没有任何手机连过主机。"
                "请确认手机 App（Dejiko定位）已启动、主机地址端口填的是 "
                + str(self.cfg.listen_host) + ":" + str(self.cfg.listen_port)
                + "，并且和主机在同一个 WiFi 下。"
            )
        lines = []
        for row in rows:
            dev_id = str(row.get("device_id"))
            bits = [
                "· " + dev_id,
                str(row.get("name") or "未命名"),
                "在线" if row.get("online") else ("离线 " + fmt.format_age(row.get("offline_seconds") or 0)),
                "ip=" + str(row.get("remote_ip") or "?"),
            ]
            age = row.get("location_age")
            loc = self.store.last_location(dev_id)
            if loc and age is not None:
                bits.append("最近位置 " + fmt.format_age(age))
                addr = str(loc.get("address") or "")
                if addr:
                    bits.append(addr)
            else:
                bits.append("还没有位置记录")
            lines.append(" | ".join(bits))
        lines.append("")
        lines.append("提示：要拿实时位置就调 locate_phone，它会下发一次定位请求让手机现场取 GPS。")
        return "\n".join(lines)

    @llm_tool(name="locate_phone")
    async def locate_phone(
        self,
        event: AstrMessageEvent,
        device_id: str = "",
        timeout: int = 0,
        force: bool = False,
        with_address: bool = True,
    ) -> str:
        """查询手机当前所在位置（实时拉取 GPS，并解析成中文地址）。

        手机不会常驻定位：调用本工具时才向手机下发一次定位请求，手机 App 现场取一次
        GPS 坐标回传，再由本工具做逆地理编码。手机 App 的前台服务必须正在运行。

        Args:
            device_id(string): 目标设备 ID；只有一台设备时留空即可。
            timeout(number): 最多等手机多少秒，0=用插件配置（默认 45 秒）。
            force(boolean): 为 true 时忽略缓存，强制让手机现场重新取一次位置。
            with_address(boolean): 是否把坐标解析成中文地址（默认 true）。
        """
        if self.server is None:
            return "插件未启用（enabled=false），手机定位通道不可用。"
        return await self._location_text(
            device_id, timeout=int(timeout or 0), force=bool(force), with_address=bool(with_address)
        )

    @llm_tool(name="where_is_phone")
    async def where_is_phone(
        self,
        event: AstrMessageEvent,
        device_id: str = "",
        with_weather: bool = True,
        timeout: int = 0,
    ) -> str:
        """一步到位：查到手机在哪、那是什么地方、当地现在什么天气。

        等价于 locate_phone + 逆地理 + 天气预报，适合"我在哪/我这边天气怎么样"这种问题。

        Args:
            device_id(string): 目标设备 ID；只有一台设备时留空即可。
            with_weather(boolean): 是否附带当地实况天气与预报（默认 true）。
            timeout(number): 最多等手机多少秒，0=用插件配置。
        """
        return await self._location_text(
            device_id, timeout=int(timeout or 0), force=False, with_address=True, with_weather=bool(with_weather)
        )

    @llm_tool(name="phone_location_history")
    async def phone_location_history(
        self, event: AstrMessageEvent, device_id: str = "", limit: int = 10
    ) -> str:
        """查看某台手机最近的位置历史（每次上报都会记一条）。

        Args:
            device_id(string): 目标设备 ID；只有一台设备时留空即可。
            limit(int): 最多返回多少条，默认 10。
        """
        dev_id, err = self.store.resolve_device(device_id)
        if err:
            return err
        rows = self.store.list_history(dev_id, limit=int(limit or 10))
        if not rows:
            return "设备 " + dev_id + " 还没有位置历史。"
        lines = ["设备 " + dev_id + " 最近 " + str(len(rows)) + " 条位置记录："]
        for rec in rows:
            lines.append("· " + time_text(rec.get("server_ts")) + "  " + fmt.fmt_location_record(rec))
        return "\n".join(lines)

    # ==================================================================
    # 工具 5~13：高德地图
    # ==================================================================
    @llm_tool(name="amap_geocode")
    async def amap_geocode(self, event: AstrMessageEvent, address: str, city: str = "") -> str:
        """把中文地址解析成经纬度坐标（高德地理编码）。

        Args:
            address(string): 结构化地址，例如「北京市朝阳区阜通东大街6号」。
            city(string): 限定城市，可提高准确率，例如「北京」；可不填。
        """
        ok, data = await self._call(self.client.geocode(address, city=city))
        if not ok:
            return str(data)
        return fmt.fmt_geocode(data)  # type: ignore[arg-type]

    @llm_tool(name="amap_reverse_geocode")
    async def amap_reverse_geocode(self, event: AstrMessageEvent, location: str) -> str:
        """把经纬度坐标解析成中文地址（高德逆地理编码，含周边 POI）。

        Args:
            location(string): 坐标，形如「116.397,39.908」，必须是 GCJ-02（高德坐标系）。
        """
        point = fmt.parse_location(location)
        if point is None:
            return "坐标格式不对，应该像 116.397,39.908 这样（经度在前）。"
        ok, data = await self._call(self.client.regeo(point[0], point[1]))
        if not ok:
            return str(data)
        lines = ["坐标 " + fmt.format_point(location), fmt.fmt_regeo(data)]  # type: ignore[arg-type]
        return "\n".join(lines)

    @llm_tool(name="amap_weather")
    async def amap_weather(self, event: AstrMessageEvent, city: str = "", forecast: bool = True) -> str:
        """查询某个城市的实况天气与未来几天预报。

        city 可以填城市名（如「杭州」）、adcode（如「330100」）或者坐标（如「120.15,30.28」）。
        留空时会用**最近一次已知的手机位置**所在地来查（不会主动向手机拉取定位）。

        Args:
            city(string): 城市名 / adcode / 坐标；留空=用手机最近已知位置。
            forecast(boolean): true 返回实况+预报，false 只返回实况。
        """
        target = str(city or "").strip()
        if not target:
            rows = self.store.list_devices()
            dev_id = ""
            for row in rows:
                did = str(row.get("device_id"))
                if self.store.last_location(did):
                    dev_id = did
                    break
            if not dev_id:
                return "没有指定城市，而且手机还没有任何位置记录，无法推断。请直接给一个城市名。"
            rec = self.store.last_location(dev_id) or {}
            target = fmt.text(rec.get("city")) or ""
            if not target:
                point = (rec.get("gcj_lng"), rec.get("gcj_lat"))
                if point[0] is not None:
                    ok, data = await self._call(self.client.regeo(float(point[0]), float(point[1])))
                    if ok:
                        comp = (data.get("regeocode") or {}).get("addressComponent")  # type: ignore[union-attr]
                        if isinstance(comp, dict):
                            target = fmt.text(comp.get("adcode")) or fmt.text(comp.get("city"))
            if not target:
                return "拿不到手机位置对应的城市，请直接指定 city。"
        else:
            point = fmt.parse_location(target)
            if point is not None:
                ok, data = await self._call(self.client.regeo(point[0], point[1]))
                if not ok:
                    return str(data)
                comp = (data.get("regeocode") or {}).get("addressComponent")  # type: ignore[union-attr]
                adcode = fmt.text(comp.get("adcode")) if isinstance(comp, dict) else ""
                if not adcode:
                    return "坐标对应的行政区没解析出来，请直接给城市名。"
                target = adcode
        extensions = "all" if bool(forecast) else "base"
        adcode = await self._to_adcode(target)
        if not adcode:
            return "查不到「" + target + "」对应的行政区编码，换个城市名或直接给 adcode 试试。"
        ok, data = await self._call(self.client.weather(adcode, extensions=extensions))
        if not ok:
            return str(data)
        return "查询对象：" + adcode + "\n" + fmt.fmt_weather(data)  # type: ignore[arg-type]

    @llm_tool(name="amap_search_place")
    async def amap_search_place(
        self,
        event: AstrMessageEvent,
        keywords: str,
        city: str = "",
        near: str = "",
        radius: int = 1000,
        page: int = 1,
    ) -> str:
        """搜索地点（POI）：找餐厅、加油站、地铁站、医院、景点等。

        给了 near 坐标就做「周边搜索」（按距离排序），否则做「城市内关键字搜索」。

        Args:
            keywords(string): 关键词，如「火锅」「充电桩」「三甲医院」。
            city(string): 限定城市名或 adcode（城市内搜索时用）。
            near(string): 中心点坐标「116.397,39.908」（GCJ-02），填了就是周边搜索。
            radius(int): 周边搜索半径（米），最大 50000，默认 1000。
            page(int): 页码，每页最多 25 条。
        """
        point = fmt.parse_location(near) if str(near or "").strip() else None
        if point is not None:
            ok, data = await self._call(
                self.client.poi_around(
                    point[0],
                    point[1],
                    keywords=keywords,
                    radius=int(radius or 1000),
                    page=int(page or 1),
                    offset=int(self.cfg.poi_page_size),
                )
            )
        else:
            if str(near or "").strip() and point is None:
                return "near 参数格式不对，应该像 116.397,39.908 这样。"
            ok, data = await self._call(
                self.client.poi_text(
                    keywords,
                    city=city,
                    page=int(page or 1),
                    offset=int(self.cfg.poi_page_size),
                )
            )
        if not ok:
            return str(data)
        return fmt.fmt_pois(data, limit=int(self.cfg.poi_page_size))  # type: ignore[arg-type]

    @llm_tool(name="amap_route")
    async def amap_route(
        self,
        event: AstrMessageEvent,
        origin: str,
        destination: str,
        mode: str = "driving",
        city: str = "",
    ) -> str:
        """路径规划：从起点到终点怎么走、多远、要多久。

        Args:
            origin(string): 起点，坐标「116.397,39.908」或中文地址都可以。
            destination(string): 终点，同上。
            mode(string): 出行方式：driving(驾车) / walking(步行) / bicycling(骑行) / transit(公交)。
            city(string): 公交模式必填，起点的城市名或 adcode，例如「北京」。
        """
        point_a = await self._point(origin)
        if isinstance(point_a, str):
            return "起点解析失败：" + point_a
        point_b = await self._point(destination)
        if isinstance(point_b, str):
            return "终点解析失败：" + point_b
        origin_str = "{:.6f},{:.6f}".format(point_a[0], point_a[1])
        dest_str = "{:.6f},{:.6f}".format(point_b[0], point_b[1])
        ok, data = await self._call(
            self.client.route(str(mode or "driving"), origin_str, dest_str, city=city, cityd=city)
        )
        if not ok:
            return str(data)
        head = (
            "路线：" + fmt.format_point(origin_str) + " → " + fmt.format_point(dest_str)
            + "（" + str(mode or "driving") + "）"
        )
        return head + "\n" + fmt.fmt_route(data, mode=str(mode or "driving"))  # type: ignore[arg-type]

    @llm_tool(name="amap_measure_distance")
    async def amap_measure_distance(
        self,
        event: AstrMessageEvent,
        origins: str,
        destination: str,
        measure_type: int = 1,
    ) -> str:
        """测量两点（或多点）之间的距离。

        Args:
            origins(string): 起点坐标，多个点用竖线分隔，如「116.397,39.908|116.4,39.9」。
            destination(string): 终点坐标「116.45,39.95」。
            measure_type(int): 1=直线距离（默认），0=驾车距离。
        """
        ok, data = await self._call(
            self.client.distance(origins=origins, destination=destination, dist_type=int(measure_type))
        )
        if not ok:
            return str(data)
        mode_text = "直线距离" if int(measure_type) == 1 else "驾车距离"
        return mode_text + "：\n" + fmt.fmt_distance_result(data)  # type: ignore[arg-type]

    @llm_tool(name="amap_ip_location")
    async def amap_ip_location(self, event: AstrMessageEvent, ip: str = "") -> str:
        """根据 IP 查归属地（省市级别，精度远不如 GPS）。

        Args:
            ip(string): 要查询的 IP；留空则查本机出口 IP 的归属地。
        """
        ok, data = await self._call(self.client.ip_locate(ip))
        if not ok:
            return str(data)
        return fmt.fmt_ip(data)  # type: ignore[arg-type]

    @llm_tool(name="amap_district")
    async def amap_district(
        self, event: AstrMessageEvent, keywords: str = "", subdistrict: int = 1
    ) -> str:
        """查询行政区划信息（省市区的 adcode、中心点坐标等）。

        Args:
            keywords(string): 行政区名称或 adcode，如「杭州」「330100」；留空返回全国省级列表。
            subdistrict(int): 返回下级行政区层数，0=不返回下级，1=返回一级，最大 3。
        """
        ok, data = await self._call(
            self.client.district(keywords=keywords, subdistrict=int(subdistrict or 0))
        )
        if not ok:
            return str(data)
        return fmt.fmt_district(data, depth=max(0, int(subdistrict or 0)) + 1)  # type: ignore[arg-type]

    @llm_tool(name="amap_static_map")
    async def amap_static_map(
        self,
        event: AstrMessageEvent,
        center: str,
        zoom: int = 0,
        markers: str = "",
    ) -> str:
        """拉取一张静态地图图片并保存到主机（想给用户看地图时用）。

        图片会存到插件数据目录，返回值里带文件路径；聊天里想直接把图发出来，
        可以让用户用命令 /amap_map <地点> 生成并发图。

        Args:
            center(string): 地图中心点坐标「116.397,39.908」（GCJ-02）或中文地址。
            zoom(int): 缩放级别 1~17，越大越细，0=用插件配置（默认 14）。
            markers(string): 标注点，格式「样式:点1;点2」，样式是 size,color,label，例如
                「mid,0xFF0000,A:116.397,39.908」。留空=自动在中心打一个红点。
        """
        point = await self._point(center)
        if isinstance(point, str):
            return "中心点解析失败：" + point
        center_str = "{:.6f},{:.6f}".format(point[0], point[1])
        mark = str(markers or "").strip() or ("mid,0xFF0000,A:" + center_str)
        ok, data = await self._call(
            self.client.static_map(
                center_str,
                zoom=int(zoom) if int(zoom or 0) > 0 else int(self.cfg.static_map_zoom),
                size=str(self.cfg.static_map_size),
                markers=mark,
            )
        )
        if not ok:
            return str(data)
        try:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.map_dir / ("map_" + stamp + ".png")
            path.write_bytes(data)  # type: ignore[arg-type]
        except Exception as exc:
            return "地图图片写入失败：" + repr(exc)
        return (
            "静态地图已生成：" + str(path) + "\n"
            "（中心 " + center_str + "，缩放 " + str(int(zoom) if int(zoom or 0) > 0 else int(self.cfg.static_map_zoom)) + "）"
        )

    @llm_tool(name="amap_status")
    async def amap_status(self, event: AstrMessageEvent) -> str:
        """自检：高德 key 配没配、手机通道通不通、有哪些设备。想排查问题就先调它。"""
        return self._status_text()

    # ==================================================================
    # 调试命令
    # ==================================================================
    def _status_text(self) -> str:
        lines = ["【astrbot_plugin_amap 状态】"]
        lines.append(self.cfg.describe())
        if self.server is None:
            lines.append("HTTP 服务：未运行（enabled=false 或启动失败）")
        else:
            lines.append(
                "HTTP 服务：运行中 http://" + str(self.cfg.listen_host) + ":" + str(self.cfg.listen_port)
            )
            lines.append("手机 App 里「主机地址」可填：")
            lines.append(netinfo.describe_addresses(int(self.cfg.listen_port)))
        lines.append("数据目录：" + str(self.data_dir))
        lines.append(
            "高德调用统计：次数=" + str(self.client.stats.get("calls", 0))
            + " 错误=" + str(self.client.stats.get("errors", 0))
            + " 最近接口=" + (str(self.client.stats.get("last_path")) or "-")
        )
        rows = self.store.list_devices()
        if not rows:
            lines.append("设备：无")
        for row in rows:
            dev_id = str(row.get("device_id"))
            seg = (
                "设备：" + dev_id
                + (" [在线]" if row.get("online") else " [离线]")
                + " ip=" + str(row.get("remote_ip") or "?")
            )
            age = row.get("location_age")
            if age is not None:
                seg += " 最近位置=" + fmt.format_age(age)
            lines.append(seg)
        lines.append("待处理定位请求：" + str(self.store.pending_count()))
        return "\n".join(lines)

    @filter.command("amap_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看高德地图插件状态。"""
        yield event.plain_result(self._status_text())

    @filter.command("amap_loc")
    async def cmd_locate(self, event: AstrMessageEvent):
        """手动拉取一次手机位置：/amap_loc [device_id]"""
        raw = str(getattr(event, "message_str", "") or "")
        tokens = [t for t in raw.split() if not t.startswith("/") and t != "amap_loc"]
        device_id = tokens[-1] if tokens else ""
        yield event.plain_result("正在向手机请求位置，请稍等…")
        text = await self._location_text(device_id, with_address=True)
        yield event.plain_result(text)

    @filter.command("amap_where")
    async def cmd_where(self, event: AstrMessageEvent):
        """一步到位查位置+天气：/amap_where [device_id]"""
        raw = str(getattr(event, "message_str", "") or "")
        tokens = [t for t in raw.split() if not t.startswith("/") and t != "amap_where"]
        device_id = tokens[-1] if tokens else ""
        yield event.plain_result("正在向手机请求位置，请稍等…")
        text = await self._location_text(device_id, with_address=True, with_weather=True)
        yield event.plain_result(text)

    @filter.command("amap_map")
    async def cmd_map(self, event: AstrMessageEvent):
        """生成并发送一张静态地图：/amap_map <地址或坐标> [zoom]"""
        raw = str(getattr(event, "message_str", "") or "")
        tokens = [t for t in raw.split() if not t.startswith("/") and t != "amap_map"]
        if not tokens:
            yield event.plain_result("用法：/amap_map <地址或坐标> [缩放1~17]")
            return
        zoom = 0
        if tokens and tokens[-1].isdigit() and len(tokens) > 1:
            zoom = int(tokens[-1])
            tokens = tokens[:-1]
        place = " ".join(tokens)
        if not self.cfg.has_key:
            yield event.plain_result(_KEY_MISSING_HINT)
            return
        point = await self._point(place)
        if isinstance(point, str):
            yield event.plain_result("解析失败：" + point)
            return
        center = "{:.6f},{:.6f}".format(point[0], point[1])
        ok, data = await self._call(
            self.client.static_map(
                center,
                zoom=zoom if zoom > 0 else int(self.cfg.static_map_zoom),
                size=str(self.cfg.static_map_size),
                markers="mid,0xFF0000,A:" + center,
            )
        )
        if not ok:
            yield event.plain_result(str(data))
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.map_dir / ("map_" + stamp + ".png")
        try:
            path.write_bytes(data)  # type: ignore[arg-type]
        except Exception as exc:
            yield event.plain_result("图片写入失败：" + repr(exc))
            return
        yield event.plain_result("地图中心：" + center + "（" + place + "）")
        yield event.image_result(str(path))

    @filter.command("amap_key")
    async def cmd_key(self, event: AstrMessageEvent):
        """检查高德 key 配置状态。"""
        if self.cfg.has_key:
            yield event.plain_result("高德 key 已配置（" + str(self.cfg.amap_key)[:4] + "****）。")
        else:
            yield event.plain_result(_KEY_MISSING_HINT)
