"""把高德返回的原始 JSON 裁剪成适合喂给 LLM 的短文本。

高德的响应字段多、空值是 `""` 或 `[]` 混着来，直接丢给模型既费 token 又容易
让它抓错字段。这里统一做「取值 → 兜底 → 组句」。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "text",
    "num",
    "clip",
    "parse_location",
    "format_point",
    "format_distance",
    "format_duration",
    "fmt_regeo",
    "fmt_geocode",
    "fmt_weather",
    "fmt_pois",
    "fmt_route",
    "fmt_distance_result",
    "fmt_district",
    "fmt_ip",
    "fmt_tips",
    "fmt_location_record",
]


def text(value: Any, default: str = "") -> str:
    """高德的空值有 None / "" / [] / {} 四种，统一成字符串。"""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        parts = [text(x) for x in value]
        parts = [p for p in parts if p]
        return "、".join(parts) if parts else default
    if isinstance(value, dict):
        return default
    result = str(value).strip()
    return result if result else default


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clip(body: str, limit: int) -> str:
    """超长截断，并明确告知被截断了。"""
    body = str(body or "")
    limit = int(limit) if limit else 0
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit] + "\n…（已截断，共 " + str(len(body)) + " 字）"


def parse_location(raw: str) -> tuple[float, float] | None:
    """解析 "116.397,39.908" 这种坐标串。"""
    body = str(raw or "").strip()
    if not body:
        return None
    body = body.replace("；", ";").replace("，", ",")
    if ";" in body:
        body = body.split(";")[0]
    if "/" in body:
        body = body.split("/")[0]
    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except Exception:
        return None


def format_point(raw: str) -> str:
    """把坐标串规范成 "116.397000,39.908000"。"""
    point = parse_location(raw)
    if point is None:
        return text(raw, "未知")
    return "{:.6f},{:.6f}".format(point[0], point[1])


def format_distance(meters: Any) -> str:
    """米 → 人话（保留必要的量级）。"""
    value = num(meters, -1.0)
    if value < 0:
        return "未知"
    if value >= 1000:
        return "{:.1f} 公里".format(value / 1000.0)
    return "{:.0f} 米".format(value)


def format_duration(seconds: Any) -> str:
    """秒 → 人话。"""
    value = num(seconds, -1.0)
    if value < 0:
        return "未知"
    total = int(value)
    if total < 60:
        return str(total) + " 秒"
    if total < 3600:
        return "{} 分 {} 秒".format(total // 60, total % 60)
    return "{} 小时 {} 分".format(total // 3600, (total % 3600) // 60)


# ---------------------------------------------------------------- 逆地理编码
def fmt_regeo(data: dict, with_pois: int = 5) -> str:
    regeo = data.get("regeocode") if isinstance(data, dict) else None
    if not isinstance(regeo, dict):
        return "逆地理编码没有返回地址信息。"
    comp = regeo.get("addressComponent")
    comp = comp if isinstance(comp, dict) else {}
    lines = ["地址：" + text(regeo.get("formatted_address"), "（无）")]

    zone_bits = [
        text(comp.get("province")),
        text(comp.get("city")),
        text(comp.get("district")),
        text(comp.get("township")),
    ]
    zone_bits = [b for b in zone_bits if b]
    if zone_bits:
        lines.append("行政区：" + " / ".join(zone_bits))

    adcode = text(comp.get("adcode"))
    citycode = text(comp.get("citycode"))
    if adcode or citycode:
        lines.append(
            "编码：adcode=" + (adcode or "?") + "  citycode=" + (citycode or "?")
        )

    neighborhood = comp.get("neighborhood")
    if isinstance(neighborhood, dict) and text(neighborhood.get("name")):
        lines.append("社区：" + text(neighborhood.get("name")) + "（" + text(neighborhood.get("type"), "?") + "）")

    building = comp.get("building")
    if isinstance(building, dict) and text(building.get("name")):
        lines.append("建筑：" + text(building.get("name")) + "（" + text(building.get("type"), "?") + "）")

    street = comp.get("streetNumber")
    if isinstance(street, dict):
        road = text(street.get("street"))
        number = text(street.get("number"))
        if road or number:
            seg = "道路：" + road + number
            if text(street.get("direction")):
                seg += "（" + text(street.get("direction"))
                if num(street.get("distance"), 0) > 0:
                    seg += " " + str(int(num(street.get("distance")))) + "米"
                seg += "）"
            lines.append(seg)

    areas = comp.get("businessAreas")
    if isinstance(areas, list) and areas:
        names = []
        for item in areas:
            if isinstance(item, dict):
                nm = text(item.get("name"))
                if nm:
                    names.append(nm)
        if names:
            lines.append("商圈：" + "、".join(names))

    pois = regeo.get("pois")
    if isinstance(pois, list) and pois and with_pois > 0:
        picks = []
        for item in pois[:with_pois]:
            if not isinstance(item, dict):
                continue
            nm = text(item.get("name"))
            if not nm:
                continue
            dist = num(item.get("distance"), -1)
            # distance==0 表示这个点就在该 POI 范围内，写「0米」反而让人误解
            if dist > 0:
                picks.append(nm + "（" + str(int(dist)) + "米）")
            else:
                picks.append(nm)
        if picks:
            lines.append("附近 POI：" + "、".join(picks))

    roads = regeo.get("roads")
    if isinstance(roads, list) and roads:
        names = []
        for item in roads[:3]:
            if isinstance(item, dict) and text(item.get("name")):
                names.append(text(item.get("name")))
        if names:
            lines.append("附近道路：" + "、".join(names))

    aois = regeo.get("aois")
    if isinstance(aois, list) and aois:
        names = []
        for item in aois[:3]:
            if isinstance(item, dict) and text(item.get("name")):
                names.append(text(item.get("name")))
        if names:
            lines.append("所属 AOI：" + "、".join(names))
    return "\n".join(lines)


# ---------------------------------------------------------------- 地理编码
def fmt_geocode(data: dict, limit: int = 5) -> str:
    items = data.get("geocodes") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return "没有解析出坐标（换个更完整的地址再试，比如带上城市和门牌号）。"
    lines = ["共 " + str(len(items)) + " 条结果："]
    for idx, item in enumerate(items[:limit], start=1):
        if not isinstance(item, dict):
            continue
        bits = [
            str(idx) + ". " + text(item.get("formatted_address"), "（无地址）"),
            "坐标 " + format_point(item.get("location")),
        ]
        zone = " / ".join(
            [x for x in [text(item.get("province")), text(item.get("city")), text(item.get("district"))] if x]
        )
        if zone:
            bits.append("行政区 " + zone)
        if text(item.get("level")):
            bits.append("匹配级别 " + text(item.get("level")))
        if text(item.get("adcode")):
            bits.append("adcode=" + text(item.get("adcode")))
        lines.append(" | ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------- 天气
def fmt_weather(data: dict) -> str:
    if not isinstance(data, dict):
        return "天气接口没有返回数据。"
    lines: list[str] = []
    lives = data.get("lives")
    if isinstance(lives, list) and lives:
        for item in lives[:3]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "【实况】" + text(item.get("city"), "?") + "："
                + text(item.get("weather"), "?")
                + "，气温 " + text(item.get("temperature"), "?") + "℃"
                + "，湿度 " + text(item.get("humidity"), "?") + "%"
                + "，" + text(item.get("winddirection"), "?") + "风 " + text(item.get("windpower"), "?") + " 级"
                + "（" + text(item.get("reporttime"), "?") + " 发布）"
            )
    forecasts = data.get("forecasts")
    if isinstance(forecasts, list) and forecasts:
        for item in forecasts[:3]:
            if not isinstance(item, dict):
                continue
            casts = item.get("casts")
            if not isinstance(casts, list) or not casts:
                continue
            lines.append("【预报】" + text(item.get("city"), "?") + "（发布 " + text(item.get("reporttime"), "?") + "）")
            for cast in casts[:4]:
                if not isinstance(cast, dict):
                    continue
                lines.append(
                    "  " + text(cast.get("date"), "?") + " " + text(cast.get("week"), "")
                    + " 白天 " + text(cast.get("dayweather"), "?")
                    + " " + text(cast.get("daytemp"), "?") + "℃"
                    + " / 夜间 " + text(cast.get("nightweather"), "?")
                    + " " + text(cast.get("nighttemp"), "?") + "℃"
                    + "，" + text(cast.get("daywind"), "?") + "风 " + text(cast.get("daypower"), "?") + " 级"
                )
    if not lines:
        return "天气接口没给出内容（可能传的城市名/ adcode 不在覆盖范围内，试试用 adcode 再查一次）。"
    return "\n".join(lines)


# ---------------------------------------------------------------- POI
def fmt_pois(data: dict, limit: int = 8) -> str:
    items = data.get("pois") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return "没有搜到 POI（换个关键词、或放宽城市/半径限制）。"
    total = text(data.get("count"), str(len(items)))
    lines = ["共 " + total + " 条，展示前 " + str(min(limit, len(items))) + " 条："]
    for idx, item in enumerate(items[:limit], start=1):
        if not isinstance(item, dict):
            continue
        name = text(item.get("name"), "（无名）")
        bits = [str(idx) + ". " + name]
        addr = text(item.get("address"))
        if addr:
            bits.append(addr)
        loc = format_point(item.get("location"))
        if loc != "未知":
            bits.append("坐标 " + loc)
        dist = num(item.get("distance"), -1)
        if dist >= 0:
            bits.append("距离 " + format_distance(dist))
        zone = "".join([text(item.get("pname")), text(item.get("cityname")), text(item.get("adname"))])
        if zone:
            bits.append("[" + zone + "]")
        tel = text(item.get("tel"))
        if tel and tel != "[]":
            bits.append("电话 " + tel)
        typecode = text(item.get("type"))
        if typecode:
            bits.append("类型 " + typecode)
        cost = ""
        biz = item.get("biz_ext")
        if isinstance(biz, dict):
            rating = text(biz.get("rating"))
            if rating:
                cost = "评分 " + rating
            price = text(biz.get("cost"))
            if price:
                cost += (" " if cost else "") + "人均 " + price
        if cost:
            bits.append(cost)
        lines.append(" | ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------- 路径规划
def fmt_route(data: dict, mode: str = "driving", limit_steps: int = 10) -> str:
    route = data.get("route") if isinstance(data, dict) else None
    if not isinstance(route, dict):
        return "路径规划没有返回结果。"
    mode = str(mode or "driving").lower()
    lines: list[str] = []

    if mode in ("transit", "bus", "公交"):
        transits = route.get("transits")
        if not isinstance(transits, list) or not transits:
            return "没有查到公交路线（换出行时间或放宽条件再试）。"
        lines.append("共 " + str(len(transits)) + " 条方案，展示前 " + str(min(3, len(transits))) + " 条：")
        for idx, plan in enumerate(transits[:3], start=1):
            if not isinstance(plan, dict):
                continue
            lines.append(
                str(idx) + ". 全程 " + format_distance(route.get("distance"))
                + "，耗时 " + format_duration(plan.get("duration"))
                + "，票价 " + text(plan.get("cost"), "?") + " 元"
                + "，步行 " + format_distance(plan.get("walking_distance"))
            )
            segments = plan.get("segments")
            if not isinstance(segments, list):
                continue
            for seg in segments[:6]:
                if not isinstance(seg, dict):
                    continue
                bus = seg.get("bus")
                if isinstance(bus, dict):
                    buslines = bus.get("buslines")
                    if isinstance(buslines, list):
                        for line in buslines[:2]:
                            if not isinstance(line, dict):
                                continue
                            dep = line.get("departure_stop")
                            arr = line.get("arrival_stop")
                            lines.append(
                                "   · 乘 " + text(line.get("name"), "?")
                                + "：" + text(dep.get("name") if isinstance(dep, dict) else "", "?")
                                + " → " + text(arr.get("name") if isinstance(arr, dict) else "", "?")
                                + "（" + str(text(line.get("via_num"), "0")) + " 站，"
                                + format_duration(line.get("duration")) + "）"
                            )
                railway = seg.get("railway")
                if isinstance(railway, dict) and text(railway.get("name")):
                    lines.append("   · 铁路 " + text(railway.get("name")) + "：" + text(railway.get("departure_stop", {}).get("name") if isinstance(railway.get("departure_stop"), dict) else "", "?"))
                walking = seg.get("walking")
                if isinstance(walking, dict) and num(walking.get("distance"), 0) > 0:
                    lines.append("   · 步行 " + format_distance(walking.get("distance")))
        return "\n".join(lines)

    paths = route.get("paths")
    if not isinstance(paths, list) or not paths:
        return "没有查到可行路线（起终点可能不在道路附近，或超出该出行方式范围）。"
    lines.append("共 " + str(len(paths)) + " 条方案，展示第 1 条：")
    best = paths[0] if isinstance(paths[0], dict) else {}
    lines.append(
        "全程 " + format_distance(best.get("distance"))
        + "，预计耗时 " + format_duration(best.get("duration"))
    )
    if mode in ("driving", "car", "驾车"):
        extra = []
        if num(best.get("tolls"), 0) > 0:
            extra.append("过路费 " + text(best.get("tolls")) + " 元")
        if num(best.get("toll_distance"), 0) > 0:
            extra.append("收费路段 " + format_distance(best.get("toll_distance")))
        if num(best.get("traffic_lights"), 0) > 0:
            extra.append("红绿灯 " + text(best.get("traffic_lights")) + " 个")
        if text(route.get("taxi_cost")):
            extra.append("打车约 " + text(route.get("taxi_cost")) + " 元")
        if extra:
            lines.append("，".join(extra))
    steps = best.get("steps")
    if isinstance(steps, list) and steps:
        lines.append("路线指引：")
        for step in steps[: max(1, int(limit_steps))]:
            if not isinstance(step, dict):
                continue
            instruction = text(step.get("instruction") or step.get("road"), "（继续前行）")
            lines.append(
                "  · " + instruction
                + "（" + format_distance(step.get("distance"))
                + " / " + format_duration(step.get("duration")) + "）"
            )
        if len(steps) > limit_steps:
            lines.append("  · …还有 " + str(len(steps) - limit_steps) + " 段")
    return "\n".join(lines)


# ---------------------------------------------------------------- 距离测量
def fmt_distance_result(data: dict) -> str:
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return "距离测量没有返回结果。"
    lines = ["共 " + str(len(items)) + " 条结果："]
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        origin = text(item.get("origin"))
        dest = text(item.get("destination"))
        # 距离测量是批量接口，顶层 status=1 不代表每条都成功，必须逐条看 code
        code = text(item.get("code"))
        info = text(item.get("info"))
        if code and code not in ("1", "10000"):
            lines.append(
                str(idx) + ". " + (origin or "起点") + " → " + (dest or "终点")
                + "：失败（" + (info or code) + "）"
            )
            continue
        lines.append(
            str(idx) + ". " + (origin or "起点")
            + " → " + (dest or "终点")
            + "：距离 " + format_distance(item.get("distance"))
            + "，耗时 " + format_duration(item.get("duration"))
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 行政区划
def fmt_district(data: dict, depth: int = 1) -> str:
    items = data.get("districts") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return "没有查到行政区信息。"
    lines: list[str] = []

    def walk(node: dict, level: int) -> None:
        if level > depth:
            return
        prefix = "  " * (level - 1)
        name = text(node.get("name"), "?")
        bits = [prefix + "· " + name]
        if text(node.get("adcode")):
            bits.append("adcode=" + text(node.get("adcode")))
        if text(node.get("citycode")) and text(node.get("citycode")) != "[]":
            bits.append("citycode=" + text(node.get("citycode")))
        if text(node.get("level")):
            bits.append(text(node.get("level")))
        center = text(node.get("center"))
        if center:
            bits.append("中心 " + format_point(center))
        lines.append(" | ".join(bits))
        children = node.get("districts")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    walk(child, level + 1)

    for item in items[:3]:
        if isinstance(item, dict):
            walk(item, 1)
    return "\n".join(lines)


# ---------------------------------------------------------------- IP 定位
def fmt_ip(data: dict) -> str:
    if not isinstance(data, dict):
        return "IP 定位没有返回数据。"
    province = text(data.get("province"), "?")
    city = text(data.get("city"), "?")
    adcode = text(data.get("adcode"), "?")
    rectangle = text(data.get("rectangle"), "")
    lines = ["IP 归属：", province + " " + city, "adcode=" + adcode]
    if rectangle:
        point = parse_location(rectangle.split(";")[0])
        if point is not None:
            lines.append("参考坐标 " + format_point(str(point[0]) + "," + str(point[1])))
    return "\n".join(lines)


# ---------------------------------------------------------------- 输入提示
def fmt_tips(data: dict, limit: int = 8) -> str:
    items = data.get("tips") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return "没有匹配的候选地点。"
    lines: list[str] = []
    for idx, item in enumerate(items[:limit], start=1):
        if not isinstance(item, dict):
            continue
        name = text(item.get("name"), "（无名）")
        bits = [str(idx) + ". " + name]
        district = text(item.get("district"))
        if district:
            bits.append(district)
        addr = text(item.get("address"))
        if addr and addr != "[]":
            bits.append(addr)
        loc = format_point(item.get("location"))
        if loc != "未知":
            bits.append("坐标 " + loc)
        lines.append(" | ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------- 手机位置记录
def fmt_location_record(rec: dict, with_age: float | None = None) -> str:
    """把一条手机位置上报表成一行文本。"""
    if not isinstance(rec, dict):
        return "（无位置数据）"
    if text(rec.get("error")):
        return "定位失败：" + text(rec.get("error"))
    lng = rec.get("lng")
    lat = rec.get("lat")
    bits = ["坐标 " + format_point(str(lng) + "," + str(lat))]
    acc = num(rec.get("accuracy"), -1)
    if acc >= 0:
        bits.append("精度 ±" + str(int(acc)) + " 米")
    provider = text(rec.get("provider"))
    if provider:
        bits.append("来源 " + provider)
    speed = num(rec.get("speed"), -1)
    if speed > 0:
        bits.append("速度 " + "{:.1f}".format(speed * 3.6) + " km/h")
    battery = rec.get("battery")
    try:
        battery_val = int(battery)  # type: ignore[arg-type]
    except Exception:
        battery_val = -1
    if battery_val >= 0:
        bits.append("电量 " + str(battery_val) + "%")
    if with_age is not None:
        bits.append("（" + format_age(with_age) + "）")
    return " | ".join(bits)


def format_age(seconds: float) -> str:
    """把「多久之前」说成人话。"""
    value = num(seconds, 0.0)
    if value < 5:
        return "刚刚"
    if value < 60:
        return str(int(value)) + " 秒前"
    if value < 3600:
        return str(int(value // 60)) + " 分钟前"
    if value < 86400:
        return str(int(value // 3600)) + " 小时前"
    return str(int(value // 86400)) + " 天前"
