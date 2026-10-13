"""astrbot_plugin_amap 离线自检（本机没有 pytest，用等价手段跑）。

用法：
    <AstrBot>\\venv\\Scripts\\python.exe tests\\selftest.py

覆盖：
    1. 全插件语法检查
    2. _conf_schema.json 与 AmapConfig 字段对齐
    3. 坐标转换（性质验证，不依赖记忆中的参考值）
    4. formatter 的裁剪与异常数据容错
    5. store 的设备表 / 位置历史 / 待处理请求 / 落盘重载
    6. 假高德服务下 AmapClient 的全部接口
    7. 定位闭环：agent 请求 → 手机 poll → 手机 report → agent 拿到坐标
    8. 端到端：插件 HTTP 服务 + 模拟手机 + 假高德 → 位置+地址+天气 一句话结果
    9. 无 key 时各工具返回友好提示而不是抛异常
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
import time
import traceback
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ASTRBOT_ROOT = PLUGIN_DIR.parent.parent.parent  # .../AstrBot
FAKE_AMAP_PORT = 16399
PLUGIN_PORT = 16310

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), str(detail)))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   <- " + str(detail)))
    return bool(ok)


def section(title: str) -> None:
    print("\n----- " + title + " -----")


# ======================================================================
# 环境准备
# ======================================================================
def bootstrap() -> None:
    if str(ASTRBOT_ROOT) not in sys.path:
        sys.path.insert(0, str(ASTRBOT_ROOT))
    if str(PLUGIN_DIR.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR.parent))
    try:
        import astrbot  # noqa: F401
    except Exception:
        print("!! 需要 AstrBot 的运行环境，请用 AstrBot\\venv\\Scripts\\python.exe 运行本脚本")
        raise SystemExit(2)
    pkg = types.ModuleType("astrbot_plugin_amap")
    pkg.__path__ = [str(PLUGIN_DIR)]
    pkg.__package__ = "astrbot_plugin_amap"
    sys.modules["astrbot_plugin_amap"] = pkg


# ======================================================================
# 1. 语法
# ======================================================================
def test_syntax() -> None:
    section("1. 语法检查")
    bad = []
    count = 0
    for path in sorted(PLUGIN_DIR.rglob("*.py")):
        if "__pycache__" in str(path) or path.name == "_probe.py":
            continue
        count += 1
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append(path.name + "::" + str(exc))
    check("语法检查（%d 个 .py）" % count, not bad, "; ".join(bad))


# ======================================================================
# 2. schema 对齐
# ======================================================================
def test_schema() -> None:
    section("2. 配置 schema 与 AmapConfig 对齐")
    from astrbot_plugin_amap.amap.config import AmapConfig

    schema_path = PLUGIN_DIR / "_conf_schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        ok = isinstance(schema, dict) and bool(schema)
    except Exception as exc:
        check("_conf_schema.json 是合法 JSON", False, repr(exc))
        return
    check("_conf_schema.json 是合法 JSON", ok, "")

    fields = set(AmapConfig.__dataclass_fields__.keys()) - {"extra"}
    missing = sorted(fields - set(schema.keys()))
    extra = sorted(set(schema.keys()) - fields)
    check("AmapConfig 字段都在 schema 里", not missing, "缺少: " + ", ".join(missing))
    check("schema 没有多余字段", not extra, "多余: " + ", ".join(extra))

    bad_types = []
    allowed = {"bool", "string", "int", "float", "list", "object", "text"}
    for key, spec in schema.items():
        if not isinstance(spec, dict) or str(spec.get("type")) not in allowed:
            bad_types.append(key)
    check("schema 每项都有合法 type/label", not bad_types, ", ".join(bad_types))

    unknown = [k for k, v in schema.items() if "default" not in v]
    check("schema 每项都有 default", not unknown, ", ".join(unknown))

    # 元信息
    meta = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    check("metadata.yaml 含 name/version/desc",
          "name: astrbot_plugin_amap" in meta and "version:" in meta and "desc:" in meta)


# ======================================================================
# 3. 坐标转换
# ======================================================================
def test_coord() -> None:
    section("3. 坐标转换")
    from astrbot_plugin_amap.amap import coord

    # 境外不动
    tokyo = coord.wgs84_to_gcj02(139.6917, 35.6895)
    check("境外坐标不做偏移", abs(tokyo[0] - 139.6917) < 1e-9 and abs(tokyo[1] - 35.6895) < 1e-9)

    # 境内偏移量量级合理（几百米，换算成度约 0.002~0.02）
    wgs = (116.397428, 39.90923)
    gcj = coord.wgs84_to_gcj02(*wgs)
    d_lng = gcj[0] - wgs[0]
    d_lat = gcj[1] - wgs[1]
    check("WGS84→GCJ02 有偏移", abs(d_lng) > 1e-6 and abs(d_lat) > 1e-6,
          "d_lng=%.8f d_lat=%.8f" % (d_lng, d_lat))
    check("偏移量在合理量级(0.001~0.02度)", 0.001 < abs(d_lng) < 0.02 and 0.001 < abs(d_lat) < 0.02,
          "d_lng=%.8f d_lat=%.8f" % (d_lng, d_lat))
    dist = coord.distance_meters(wgs[0], wgs[1], gcj[0], gcj[1])
    check("偏移距离在 100~800 米之间", 100 < dist < 800, "%.1f 米" % dist)

    # 往返自洽
    back = coord.gcj02_to_wgs84(*gcj)
    err = coord.distance_meters(wgs[0], wgs[1], back[0], back[1])
    check("GCJ02→WGS84 往返误差 < 1 米", err < 1.0, "%.4f 米" % err)

    # convert_point 各组合
    same = coord.convert_point(116.4, 39.9, "gcj02", "gcj02")
    check("同坐标系原样返回", same == (116.4, 39.9))
    bd = coord.convert_point(116.404, 39.915, "gcj02", "bd09")
    back_bd = coord.convert_point(bd[0], bd[1], "bd09", "gcj02")
    err2 = coord.distance_meters(116.404, 39.915, back_bd[0], back_bd[1])
    check("GCJ02↔BD09 往返误差 < 1 米", err2 < 1.0, "%.4f 米" % err2)
    w2g = coord.convert_point(116.397428, 39.90923, "wgs84", "gcj02")
    check("convert_point 支持别名(gps/amap)",
          abs(w2g[0] - gcj[0]) < 1e-9 and abs(w2g[1] - gcj[1]) < 1e-9)
    check("非法坐标不炸", coord.wgs84_to_gcj02(999.0, 999.0) == (999.0, 999.0))


# ======================================================================
# 4. formatter
# ======================================================================
def test_formatter() -> None:
    section("4. formatter")
    from astrbot_plugin_amap.amap import formatter as fmt

    check("parse_location 正常解析", fmt.parse_location("116.397,39.908") == (116.397, 39.908))
    check("parse_location 中文逗号", fmt.parse_location("116.397，39.908") == (116.397, 39.908))
    check("parse_location 多点取首个", fmt.parse_location("116.397,39.908;120,30") == (116.397, 39.908))
    check("parse_location 非法返回 None", fmt.parse_location("北京") is None)
    check("format_distance", fmt.format_distance("1234") == "1.2 公里" and fmt.format_distance(500) == "500 米")
    check("format_duration", fmt.format_duration(90) == "1 分 30 秒" and fmt.format_duration(3700).startswith("1 小时"))
    check("text() 处理空数组", fmt.text([]) == "" and fmt.text(["a", "b"]) == "a、b")

    # 空数据不炸
    for fn in (fmt.fmt_regeo, fmt.fmt_geocode, fmt.fmt_weather, fmt.fmt_pois,
               fmt.fmt_route, fmt.fmt_distance_result, fmt.fmt_district, fmt.fmt_ip, fmt.fmt_tips):
        try:
            fn({})
            out = fn(None)  # type: ignore[arg-type]
            ok = isinstance(out, str)
        except Exception as exc:
            ok = False
            out = repr(exc)
        if not ok:
            check("空输入容错 " + fn.__name__, False, str(out))
            break
    else:
        check("所有 fmt_* 在空输入下都不抛异常", True)

    # 真实结构
    regeo = {
        "status": "1",
        "regeocode": {
            "formatted_address": "北京市东城区东华门街道天安门",
            "addressComponent": {
                "province": "北京市", "city": [], "district": "东城区",
                "adcode": "110101", "citycode": "010", "township": "东华门街道",
                "streetNumber": {"street": "长安街", "number": "1号", "distance": "10"},
                "businessAreas": [{"name": "天安门"}],
            },
            "pois": [{"name": "天安门广场", "distance": "120"}],
            "roads": [{"name": "长安街"}],
        },
    }
    out = fmt.fmt_regeo(regeo)
    check("fmt_regeo 含地址与 adcode", "天安门" in out and "110101" in out, out[:80])
    check("fmt_regeo 处理 city 为空数组", "北京市 / 东城区" in out or "北京市" in out, out[:120])

    wx = {"lives": [{"city": "北京市", "weather": "晴", "temperature": "26", "humidity": "40",
                     "winddirection": "南", "windpower": "≤3", "reporttime": "2026-10-13 19:00:00"}],
          "forecasts": [{"city": "北京市", "reporttime": "2026-10-13 19:00:00",
                         "casts": [{"date": "2026-10-13", "week": "2", "dayweather": "晴",
                                    "nightweather": "多云", "daytemp": "27", "nighttemp": "16",
                                    "daywind": "南", "daypower": "1-3"}]}]}
    out = fmt.fmt_weather(wx)
    check("fmt_weather 含实况与预报", "晴" in out and "26" in out and "预报" in out, out[:120])

    pois = {"count": "2", "pois": [
        {"name": "海底捞", "address": "某路1号", "location": "116.4,39.9", "distance": "300",
         "pname": "北京市", "cityname": "北京市", "adname": "东城区", "tel": "010-123",
         "type": "餐饮服务", "biz_ext": {"rating": "4.8", "cost": "120"}},
        {"name": "星巴克", "location": "116.41,39.91"},
    ]}
    out = fmt.fmt_pois(pois)
    check("fmt_pois 含名称/距离/评分", "海底捞" in out and "300" in out and "4.8" in out, out[:160])

    route = {"route": {"taxi_cost": "20", "paths": [{
        "distance": "12000", "duration": "1800", "tolls": "10", "traffic_lights": "12",
        "steps": [{"instruction": "向东行驶", "road": "长安街", "distance": "500", "duration": "60"}]}]}}
    out = fmt.fmt_route(route, "driving")
    check("fmt_route 驾车含距离/耗时/过路费", "12.0 公里" in out and "30 分" in out and "过路费" in out, out[:200])

    transit = {"route": {"distance": "9000", "transits": [{
        "cost": "5", "duration": "2400", "walking_distance": "800",
        "segments": [{"bus": {"buslines": [{"name": "地铁1号线", "via_num": "5", "duration": "900",
                                            "departure_stop": {"name": "A站"}, "arrival_stop": {"name": "B站"}}]}}]}]}}
    out = fmt.fmt_route(transit, "transit")
    check("fmt_route 公交含线路名与站点", "地铁1号线" in out and "A站" in out, out[:200])

    rec = {"lng": 116.4, "lat": 39.9, "accuracy": 12.5, "provider": "gps", "speed": 3.6, "battery": 88}
    out = fmt.fmt_location_record(rec)
    check("fmt_location_record 含坐标/精度/电量", "116.400000" in out and "±12" in out and "88%" in out, out)

    # regeo 里 distance==0 表示「就在这个 POI 范围内」，写 0 米会误导
    out = fmt.fmt_regeo({"regeocode": {"addressComponent": {}, "pois": [{"name": "某大楼", "distance": "0"}]}})
    check("regeo 的 distance=0 不显示成「0米」", "某大楼" in out and "0米）" not in out, out)

    # 距离测量是批量接口，顶层成功不代表每条都成功
    dist_bad = {"results": [
        {"origin": "116.4,39.9", "destination": "116.5,39.95", "code": "1",
         "distance": "1000", "duration": "60"},
        {"origin": "116.6,39.9", "destination": "116.5,39.95", "code": "30003",
         "info": "NO_ROADS_NEARBY"},
    ]}
    out = fmt.fmt_distance_result(dist_bad)
    check("fmt_distance_result 逐条识别失败", "1.0 公里" in out and "NO_ROADS_NEARBY" in out, out)


# ======================================================================
# 5. store
# ======================================================================
def test_store() -> None:
    section("5. store")
    import tempfile

    from astrbot_plugin_amap.amap.config import AmapConfig
    from astrbot_plugin_amap.amap.store import LocationStore

    with tempfile.TemporaryDirectory() as tmp:
        cfg = AmapConfig.from_dict({"persist": True, "history_limit": 5, "device_offline_sec": 60,
                                    "auto_convert": True})
        st = LocationStore(Path(tmp), cfg)
        check("初始没有设备", st.list_devices() == [])

        st.touch_device("dev-a", name="Away的手机", remote_ip="192.168.1.5")
        st.touch_device("dev-b", name="备用机", remote_ip="192.168.1.6")
        check("设备登记成功", st.device_exists("dev-a") and st.device_exists("dev-b"))
        check("设备在线判定", st.is_online("dev-a"))

        # 位置
        rec = st.save_location("dev-a", {"lng": 116.397428, "lat": 39.90923, "accuracy": 10,
                                         "provider": "gps", "ts": 1.0})
        check("保存位置后能读回", st.last_location("dev-a") is not None)
        check("自动做了 WGS84→GCJ02", abs(float(rec["gcj_lng"]) - float(rec["wgs_lng"])) > 1e-6,
              "wgs=%s gcj=%s" % (rec["wgs_lng"], rec["gcj_lng"]))

        st.update_location_meta("dev-a", {"address": "北京市东城区天安门", "adcode": "110101", "city": "北京市"})
        check("地址回写后 last_location 里能看到", "天安门" in str(st.last_location("dev-a").get("address")))
        check("历史里同一条也被更新",
              "天安门" in str(st.list_history("dev-a", 5)[-1].get("address")))

        # 历史上限
        for i in range(10):
            st.save_location("dev-a", {"lng": 116.4 + i * 0.001, "lat": 39.9, "ts": float(i)})
        check("历史按 history_limit 截断", len(st.list_history("dev-a", 100)) == 5,
              "实际 %d 条" % len(st.list_history("dev-a", 100)))

        # 待处理请求
        ev = asyncio.Event()
        st.add_pending("req-1", {"id": "req-1", "device_id": "dev-a", "created": time.time(),
                                 "ttl": 999, "event": ev, "result": None})
        check("peek_pending_for 命中", (st.peek_pending_for("dev-a") or {}).get("id") == "req-1")
        check("peek_pending_for 不会误命中别的设备", st.peek_pending_for("dev-b") is None)
        check("mark_dispatched 计数", st.mark_dispatched("req-1") and st.mark_dispatched("req-1"))
        check("resolve_pending 唤醒等待者", st.resolve_pending("req-1", {"lng": 1.0}) and ev.is_set())
        check("resolve_pending 未知请求返回 False", st.resolve_pending("nope", {}) is False)
        check("pending_count", st.pending_count() == 1)
        st.drop_pending("req-1")
        check("drop_pending 生效", st.pending_count() == 0)

        # 落盘重载
        st.flush()
        st2 = LocationStore(Path(tmp), cfg)
        check("重启后设备还在", st2.device_exists("dev-a") and st2.device_exists("dev-b"))
        check("重启后位置还在", st2.last_location("dev-a") is not None)

        # 设备解析
        dev, err = st.resolve_device("dev-a")
        check("resolve_device 指定存在设备", dev == "dev-a" and not err)
        dev, err = st.resolve_device("不存在")
        check("resolve_device 未知设备给提示", dev == "" and "没见过设备" in err)
        dev, err = st.resolve_device("")
        check("resolve_device 多设备时要求指定", dev == "" and "必须指定" in err)
        st2.devices.pop("dev-b", None)
        dev, err = st2.resolve_device("")
        check("resolve_device 单设备时自动选它", dev == "dev-a" and not err, str((dev, err)))


# ======================================================================
# 假高德服务
# ======================================================================
def build_fake_amap_app():
    from aiohttp import web

    async def geocode(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000", "count": "1",
            "geocodes": [{"formatted_address": "北京市朝阳区阜通东大街6号",
                          "province": "北京市", "city": "北京市", "district": "朝阳区",
                          "adcode": "110105", "location": "116.482,39.996", "level": "门牌号"}],
        })

    async def regeo(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000",
            "regeocode": {
                "formatted_address": "北京市东城区东华门街道天安门广场",
                "addressComponent": {
                    "province": "北京市", "city": [], "citycode": "010", "district": "东城区",
                    "adcode": "110101", "township": "东华门街道",
                    "neighborhood": {"name": "天安门", "type": "地名地址信息"},
                    "building": {"name": "天安门城楼", "type": "风景名胜"},
                    "streetNumber": {"street": "东长安街", "number": "1号", "distance": "20"},
                    "businessAreas": [{"name": "天安门商圈"}],
                },
                "pois": [{"name": "天安门广场", "distance": "120", "type": "风景名胜"}],
                "roads": [{"name": "东长安街"}],
                "aois": [{"name": "天安门广场"}],
            },
        })

    async def weather(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000", "count": "1",
            "lives": [{"province": "北京市", "city": "北京市", "adcode": "110101", "weather": "晴",
                       "temperature": "26", "winddirection": "南", "windpower": "≤3",
                       "humidity": "40", "reporttime": "2026-10-13 19:00:00"}],
            "forecasts": [{"city": "北京市", "adcode": "110101", "province": "北京市",
                           "reporttime": "2026-10-13 19:00:00",
                           "casts": [{"date": "2026-10-13", "week": "2", "dayweather": "晴",
                                      "nightweather": "多云", "daytemp": "27", "nighttemp": "16",
                                      "daywind": "南", "daypower": "1-3"}]}],
        })

    async def place_text(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000", "count": "1",
            "pois": [{"id": "B000A83M61", "name": "海底捞火锅(朝阳店)", "type": "餐饮服务",
                      "address": "阜通东大街6号", "location": "116.483,39.997", "tel": "010-11111111",
                      "pname": "北京市", "cityname": "北京市", "adname": "朝阳区",
                      "biz_ext": {"rating": "4.8", "cost": "128"}}],
        })

    async def place_around(request):
        body = await place_text(request)
        return body

    async def distance(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000",
            "results": [{"origin_id": "1", "dest_id": "1", "distance": "12345", "duration": "1500"}],
        })

    async def driving(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000",
            "route": {"origin": "116.4,39.9", "destination": "116.5,39.95", "taxi_cost": "45",
                      "paths": [{"distance": "18000", "duration": "2400", "tolls": "10",
                                 "toll_distance": "3000", "traffic_lights": "15",
                                 "steps": [{"instruction": "沿长安街向东行驶", "road": "长安街",
                                            "distance": "2000", "duration": "300"}]}]},
        })

    async def transit(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000",
            "route": {"distance": "18000", "transits": [{
                "cost": "6", "duration": "3600", "walking_distance": "900",
                "segments": [{"bus": {"buslines": [{"name": "地铁1号线", "via_num": "6", "duration": "1200",
                                                    "departure_stop": {"name": "天安门东"},
                                                    "arrival_stop": {"name": "国贸"}}]}}]}]},
        })

    async def ip(request):
        return web.json_response({"status": "1", "info": "OK", "infocode": "10000",
                                  "province": "北京市", "city": "北京市", "adcode": "110000",
                                  "rectangle": "116.0119343,39.66127144;116.7829835,40.2164962"})

    async def district(request):
        return web.json_response({
            "status": "1", "info": "OK", "infocode": "10000",
            "districts": [{"name": "北京市", "adcode": "110000", "citycode": "010",
                           "center": "116.407526,39.904030", "level": "province",
                           "districts": [{"name": "东城区", "adcode": "110101", "citycode": "010",
                                          "center": "116.418757,39.917544", "level": "district",
                                          "districts": []}]}],
        })

    async def staticmap(request):
        # 1x1 透明 PNG
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )
        return web.Response(body=png, content_type="image/png")

    async def staticmap_bad(request):
        # 静态地图出错时返回的是一段 JSON 文本，插件必须认出来而不是存成图
        return web.json_response({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})

    app = web.Application()
    app.router.add_get("/v3/geocode/geo", geocode)
    app.router.add_get("/v3/geocode/regeo", regeo)
    app.router.add_get("/v3/weather/weatherInfo", weather)
    app.router.add_get("/v3/place/text", place_text)
    app.router.add_get("/v3/place/around", place_around)
    app.router.add_get("/v3/distance", distance)
    app.router.add_get("/v3/direction/driving", driving)
    app.router.add_get("/v3/direction/transit/integrated", transit)
    app.router.add_get("/v3/ip", ip)
    app.router.add_get("/v3/config/district", district)
    app.router.add_get("/v3/staticmap", staticmap)
    app.router.add_get("/v3/staticmap_bad", staticmap_bad)
    return app


async def start_fake_amap():
    from aiohttp import web

    runner = web.AppRunner(build_fake_amap_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", FAKE_AMAP_PORT)
    await site.start()
    return runner


# ======================================================================
# 6. AmapClient
# ======================================================================
async def test_client() -> None:
    section("6. AmapClient（对手写的假高德服务）")
    from astrbot_plugin_amap.amap.client import AmapClient, AmapError
    from astrbot_plugin_amap.amap.config import AmapConfig

    cfg = AmapConfig.from_dict({
        "amap_key": "FAKEKEY1234567890",
        "api_base": "http://127.0.0.1:%d" % FAKE_AMAP_PORT,
        "request_timeout": 5,
        "retry_times": 0,
    })
    client = AmapClient(cfg)
    try:
        data = await client.geocode("北京市朝阳区阜通东大街6号")
        geos = data.get("geocodes") or []
        check("geocode 返回坐标", geos and geos[0]["location"] == "116.482,39.996")

        data = await client.regeo(116.397, 39.908)
        check("regeo 返回地址", "天安门" in str(data.get("regeocode", {}).get("formatted_address")))

        data = await client.weather("110101")
        check("weather 返回实况", (data.get("lives") or [{}])[0].get("weather") == "晴")

        data = await client.poi_text("火锅", city="北京")
        check("poi_text 返回 POI", (data.get("pois") or [{}])[0].get("name", "").startswith("海底捞"))

        data = await client.poi_around(116.4, 39.9, keywords="火锅", radius=2000)
        check("poi_around 返回 POI", bool(data.get("pois")))

        data = await client.route("driving", "116.4,39.9", "116.5,39.95")
        check("route(driving) 返回路径", bool(data.get("route", {}).get("paths")))

        data = await client.route("transit", "116.4,39.9", "116.5,39.95", city="北京")
        check("route(transit) 返回方案", bool(data.get("route", {}).get("transits")))

        data = await client.distance("116.4,39.9", "116.5,39.95", dist_type=1)
        check("distance 返回结果", (data.get("results") or [{}])[0].get("distance") == "12345")

        data = await client.ip_locate("8.8.8.8")
        check("ip 定位返回省份", data.get("province") == "北京市")

        data = await client.district("北京")
        check("district 返回行政区", (data.get("districts") or [{}])[0].get("adcode") == "110000")

        blob = await client.static_map("116.4,39.9")
        check("static_map 返回 PNG 字节", blob[:8] == b"\x89PNG\r\n\x1a\n", str(blob[:8]))

        try:
            await client._request("/v3/staticmap_bad", {"location": "1,1"}, raw=True)
            check("静态地图返回 JSON 错误时抛异常", False, "居然没抛异常")
        except AmapError as exc:
            check("静态地图返回 JSON 错误时抛异常（不会把报错存成图）", exc.code == "10001", exc.code)

        # 缓存：高德配额按月算，搜索类只有 5000/月，重复查询必须挡住
        before = client.stats["calls"]
        await client.geocode("缓存测试地址")
        first = client.stats["calls"]
        await client.geocode("缓存测试地址")
        second = client.stats["calls"]
        check("同一查询第二次直接命中缓存", second == first and first > before,
              "before=%d first=%d second=%d" % (before, first, second))
        check("缓存命中被计数", client.cache.hits >= 1, str(client.cache.hits))

        # 数字签名
        cfg2 = AmapConfig.from_dict({"amap_key": "K", "amap_sig_secret": "SECRET"})
        c2 = AmapClient(cfg2)
        sig = c2._sign({"b": "2", "a": "1", "key": "K"})
        import hashlib
        expect = hashlib.md5(b"a=1&b=2&key=KSECRET").hexdigest()
        check("数字签名按 key 升序拼接（含 key 参数）", sig == expect)
        await c2.close()

        # 无 key
        cfg3 = AmapConfig.from_dict({"amap_key": ""})
        c3 = AmapClient(cfg3)
        try:
            await c3.geocode("北京")
            check("无 key 时抛 AmapError(NO_KEY)", False, "居然没抛异常")
        except AmapError as exc:
            check("无 key 时抛 AmapError(NO_KEY)", exc.code == "NO_KEY", exc.code)
        await c3.close()

        # 错误码归一化：指向一个没人监听的端口
        cfg4 = AmapConfig.from_dict({"amap_key": "K", "api_base": "http://127.0.0.1:1",
                                     "retry_times": 0, "request_timeout": 2})
        c4 = AmapClient(cfg4)
        try:
            await c4.geocode("北京")
            check("网络不通时抛 AmapError", False, "居然没抛异常")
        except AmapError as exc:
            check("网络不通时抛 AmapError", exc.code in ("NETWORK", "TIMEOUT"), exc.code)
        await c4.close()
        check("错误可读文本", "高德接口调用失败" in AmapError("10001", "INVALID_USER_KEY", "/x").human())

        check("调用统计有记录", client.stats["calls"] >= 11, str(client.stats["calls"]))
    finally:
        await client.close()


# ======================================================================
# 7. 定位闭环
# ======================================================================
async def test_locate_loop() -> None:
    section("7. 定位闭环（locator）")
    import tempfile

    from astrbot_plugin_amap.amap.client import AmapClient
    from astrbot_plugin_amap.amap.config import AmapConfig
    from astrbot_plugin_amap.amap.locator import LocateManager
    from astrbot_plugin_amap.amap.store import LocationStore

    with tempfile.TemporaryDirectory() as tmp:
        cfg = AmapConfig.from_dict({
            "persist": False, "locate_timeout": 5, "locate_max_age": 0,
            "device_offline_sec": 60, "auto_convert": True,
        })
        st = LocationStore(Path(tmp), cfg)
        st.touch_device("dev-x", name="测试机", remote_ip="127.0.0.1")
        loc = LocateManager(cfg, st, AmapClient(cfg))

        # 后台等定位
        task = asyncio.create_task(loc.locate("dev-x", timeout=5))
        await asyncio.sleep(0.2)

        pending = loc.poll_for_device("dev-x")
        check("手机轮询拿到定位请求", bool(pending) and pending["id"].startswith("loc-"), str(pending))

        repeated = [loc.poll_for_device("dev-x") for _ in range(4)]
        check("同一请求最多派发 3 次",
              sum(1 for x in repeated if x) == 2,
              "又派发了 %d 次" % sum(1 for x in repeated if x))

        # 手机上报
        rep = loc.handle_report({
            "device_id": "dev-x", "request_id": pending["id"],
            "lng": 116.397428, "lat": 39.90923, "accuracy": 8.0,
            "provider": "gps", "battery": 77, "ts": 1000.0,
        })
        check("上报被接受且匹配到请求", rep["matched"] is True)

        result = await asyncio.wait_for(task, timeout=3)
        check("locate 拿到结果", result.get("ok") is True, str(result))
        check("结果来自实时拉取", result.get("from") == "fresh")
        rec = result.get("location") or {}
        check("上报坐标被转成 GCJ02", abs(float(rec.get("gcj_lng")) - float(rec.get("wgs_lng"))) > 1e-6,
              "wgs=%s gcj=%s" % (rec.get("wgs_lng"), rec.get("gcj_lng")))

        # 缓存复用
        cfg.locate_max_age = 60
        cached = await loc.locate("dev-x", timeout=1)
        check("缓存新鲜时不再骚扰手机", cached.get("from") == "cache", str(cached.get("from")))

        # 手机上报失败
        cfg.locate_max_age = 0
        task2 = asyncio.create_task(loc.locate("dev-x", timeout=5))
        await asyncio.sleep(0.2)
        req2 = loc.poll_for_device("dev-x")
        loc.handle_report({"device_id": "dev-x", "request_id": req2["id"], "error": "定位服务被关闭"})
        r2 = await asyncio.wait_for(task2, timeout=3)
        check("手机报错时定位返回失败并带上原因",
              r2.get("ok") is False and "定位服务被关闭" in str(r2.get("error")), str(r2))

        # 超时
        cfg.locate_timeout = 1
        r3 = await loc.locate("dev-x", timeout=1)
        check("手机不回传时超时返回", r3.get("ok") is False and "没有在" in str(r3.get("error")), str(r3))

        # 设备离线
        st.devices["dev-x"]["last_seen"] = 0
        r4 = await loc.locate("dev-x", timeout=1)
        check("设备离线时立刻返回提示", r4.get("ok") is False and "没有轮询主机" in str(r4.get("error")), str(r4))

        r5 = await loc.locate("不存在的设备", timeout=1)
        check("未知设备返回提示", r5.get("ok") is False and "没见过设备" in str(r5.get("error")), str(r5))


# ======================================================================
# 8. 端到端
# ======================================================================
async def test_end_to_end() -> None:
    section("8. 端到端（插件 HTTP 服务 + 模拟手机 + 假高德）")
    import tempfile

    from aiohttp import ClientSession

    from astrbot_plugin_amap.main import AmapPlugin

    class FakeContext:
        def __getattr__(self, item):
            def _any(*a, **kw):
                return None
            return _any

    with tempfile.TemporaryDirectory() as tmp:
        cfg = {
            "amap_key": "FAKEKEY1234567890",
            "api_base": "http://127.0.0.1:%d" % FAKE_AMAP_PORT,
            "listen_host": "127.0.0.1",
            "listen_port": PLUGIN_PORT,
            "persist": True,
            "locate_timeout": 8,
            "locate_max_age": 60,
            "history_limit": 20,
            "device_offline_sec": 120,
            "static_map_dir": str(Path(tmp) / "maps"),
        }
        plugin = AmapPlugin(FakeContext(), cfg)
        plugin.data_dir = Path(tmp)
        plugin.store.data_dir = Path(tmp)
        plugin.store.state_file = Path(tmp) / "state.json"
        await plugin.initialize()
        check("插件 HTTP 服务已启动", plugin.server is not None)

        base = "http://127.0.0.1:%d" % PLUGIN_PORT
        async with ClientSession() as http:
            # 1) 手机 ping
            async with http.get(base + "/amap/ping", params={"device_id": "phone-1", "name": "Away的手机"}) as r:
                ping = await r.json()
            check("GET /amap/ping 正常", r.status == 200 and ping.get("ok") and ping.get("name") == "astrbot_plugin_amap",
                  str(ping))
            check("ping 带出建议轮询间隔", isinstance(ping.get("poll_interval"), int))

            # 2) 没有请求时 poll 返回 null
            async with http.get(base + "/amap/poll", params={"device_id": "phone-1"}) as r:
                poll0 = await r.json()
            check("空闲时 /amap/poll 返回 request=null", poll0.get("request") is None, str(poll0))

            # 3) agent 要位置（后台），手机 poll 取到请求并上报
            locate_task = asyncio.create_task(plugin._location_text("phone-1", timeout=8))
            await asyncio.sleep(0.3)
            async with http.get(base + "/amap/poll", params={"device_id": "phone-1"}) as r:
                poll1 = await r.json()
            req = poll1.get("request") or {}
            check("手机 poll 取到定位请求", bool(req.get("id")), str(poll1))
            check("请求里带 timeout_ms / high_accuracy",
                  isinstance(req.get("timeout_ms"), int) and "high_accuracy" in req, str(req))

            async with http.post(base + "/amap/report", json={
                "device_id": "phone-1", "request_id": req.get("id"),
                "lng": 116.397428, "lat": 39.90923, "accuracy": 6.5, "provider": "gps",
                "altitude": 45.0, "speed": 0.0, "bearing": 0.0, "ts": 1770000000.0, "battery": 66,
            }) as r:
                rep = await r.json()
            check("POST /amap/report 被接受并匹配", rep.get("ok") and rep.get("matched") is True, str(rep))

            text = await asyncio.wait_for(locate_task, timeout=10)
            check("_location_text 返回位置+地址", "坐标" in text and "天安门" in text, text[:200])
            check("返回里带精度与电量", "±6" in text and "66%" in text, text[:200])

            # 4) 位置+天气一步到位
            where = await plugin._location_text("phone-1", with_address=True, with_weather=True)
            check("where 版带当地天气", "晴" in where and "26" in where, where[-260:])

            # 5) GET /amap/last
            async with http.get(base + "/amap/last", params={"device_id": "phone-1"}) as r:
                last = await r.json()
            check("GET /amap/last 返回最近位置", last.get("ok") and last.get("location"), str(last)[:160])

            # 6) GET /amap/history
            async with http.get(base + "/amap/history", params={"device_id": "phone-1", "limit": 5}) as r:
                hist = await r.json()
            check("GET /amap/history 返回历史", hist.get("ok") and len(hist.get("history") or []) >= 1, str(hist)[:160])

            # 7) GET /amap/health
            async with http.get(base + "/amap/health") as r:
                health = await r.json()
            check("GET /amap/health 自检正常",
                  health.get("ok") and health.get("amap_key_configured") is True, str(health))

            # 8) 坐标非法被拒
            async with http.post(base + "/amap/report", json={
                "device_id": "phone-1", "lng": 999.0, "lat": 39.0}) as r:
                bad = await r.json()
            check("非法坐标被 400 拒绝", r.status == 400 and bad.get("ok") is False, str(bad))

            # 9) 缺 device_id 的 poll
            async with http.get(base + "/amap/poll") as r:
                check("poll 缺 device_id 返回 400", r.status == 400)

            # 10) 根路径说明页
            async with http.get(base + "/") as r:
                root_text = await r.text()
            check("根路径可访问", r.status == 200 and "astrbot_plugin_amap" in root_text)

            # 11) 完全照安卓 App Reporter.buildPayload 的字段再打一遍
            #     （主动上报：request_id 为空串；这就是 App 上「立即上报一次位置」的行为）
            async with http.post(base + "/amap/report", json={
                "device_id": "phone-1", "request_id": "",
                "lng": 116.310, "lat": 39.990, "accuracy": 22.0, "provider": "network",
                "altitude": 0.0, "speed": 0.0, "bearing": 0.0,
                "ts": time.time(), "battery": -1, "error": "",
            }) as r:
                rep = await r.json()
            check("照 App 字段主动上报（request_id 空串、battery=-1）",
                  rep.get("ok") and rep.get("matched") is False, str(rep))

            # 12) App 定位失败时的上报：lng=lat=0 + error 文本，必须被接受且不污染位置
            async with http.post(base + "/amap/report", json={
                "device_id": "phone-1", "request_id": "",
                "lng": 0.0, "lat": 0.0, "accuracy": 0.0, "provider": "unknown",
                "altitude": 0.0, "speed": 0.0, "bearing": 0.0,
                "ts": time.time(), "battery": 55, "error": "定位超时：GPS 与网络都没拿到点",
            }) as r:
                rep = await r.json()
            check("定位失败上报（0,0 + error）被接受而不是 400", rep.get("ok") is True, str(rep))
            recent = plugin.store.last_location("phone-1") or {}
            check("失败上报不覆盖最近一次有效位置",
                  abs(float(recent.get("wgs_lat", 0)) - 39.990) < 0.01, str(recent)[:180])

            # 13) 没有 error 但坐标是 0,0（客户端漏填 error 的兜底）
            async with http.post(base + "/amap/report", json={
                "device_id": "phone-1", "request_id": "",
                "lng": 0.0, "lat": 0.0, "accuracy": 0.0, "provider": "gps",
                "ts": time.time(), "battery": 55, "error": "",
            }) as r:
                rep = await r.json()
            check("裸 0,0 也被当成定位失败拦下", rep.get("ok") is True, str(rep))
            recent = plugin.store.last_location("phone-1") or {}
            check("裸 0,0 不会覆盖有效位置",
                  abs(float(recent.get("wgs_lat", 0)) - 39.990) < 0.01, str(recent)[:180])

        # 11) 各 LLM 工具对着假高德跑一遍
        check("工具 amap_geocode", "116.482" in await plugin.amap_geocode(None, "北京市朝阳区阜通东大街6号"))
        check("工具 amap_reverse_geocode", "天安门" in await plugin.amap_reverse_geocode(None, "116.397,39.908"))
        check("工具 amap_reverse_geocode 传错格式给提示",
              "格式不对" in await plugin.amap_reverse_geocode(None, "北京"))
        check("工具 amap_weather", "晴" in await plugin.amap_weather(None, "110101"))
        wx_text = await plugin.amap_weather(None, "北京市")
        check("中文城市名先翻成 adcode 再查天气", "查询对象：110000" in wx_text, wx_text[:120])
        check("工具 amap_weather 用手机位置推断城市",
              "晴" in await plugin.amap_weather(None, ""), "（应回落到最近位置的城市）")
        check("工具 amap_search_place", "海底捞" in await plugin.amap_search_place(None, "火锅", city="北京"))
        check("工具 amap_search_place 周边搜索",
              "海底捞" in await plugin.amap_search_place(None, "火锅", near="116.4,39.9", radius=2000))
        route_text = await plugin.amap_route(None, "116.4,39.9", "116.5,39.95", mode="driving")
        check("工具 amap_route 驾车", "18.0 公里" in route_text and "长安街" in route_text, route_text[:160])
        route_text = await plugin.amap_route(None, "天安门", "国贸", mode="transit", city="北京")
        check("工具 amap_route 公交（起终点用地址）", "地铁1号线" in route_text, route_text[:200])
        check("工具 amap_measure_distance",
              "12.3 公里" in await plugin.amap_measure_distance(None, "116.4,39.9", "116.5,39.95"))
        check("工具 amap_ip_location", "北京市" in await plugin.amap_ip_location(None, "8.8.8.8"))
        check("工具 amap_district", "东城区" in await plugin.amap_district(None, "北京", subdistrict=1))
        map_text = await plugin.amap_static_map(None, "116.397,39.908", zoom=15)
        check("工具 amap_static_map 落盘", "已生成" in map_text and Path(map_text.split("：")[1].split("\n")[0]).exists(),
              map_text[:160])
        check("工具 list_phone_devices 有设备", "phone-1" in await plugin.list_phone_devices(None))
        check("工具 phone_location_history", "phone-1" in await plugin.phone_location_history(None, "phone-1", 5))
        check("工具 where_is_phone", "晴" in await plugin.where_is_phone(None, "phone-1"))
        check("工具 amap_status 自检", "astrbot_plugin_amap 状态" in await plugin.amap_status(None))

        # 12) 无 key 时全部工具给友好提示
        cfg_nokey = {"amap_key": "", "persist": False, "listen_port": PLUGIN_PORT + 1}
        p2 = AmapPlugin(FakeContext(), cfg_nokey)
        p2.data_dir = Path(tmp)
        out = await p2.amap_geocode(None, "北京")
        check("无 key 时 amap_geocode 给配置提示", "还没有配置高德 Web 服务 key" in out, out[:120])
        out = await p2.amap_route(None, "北京", "上海")
        check("无 key 时 amap_route 给配置提示", "还没有配置" in out, out[:120])
        out = await p2.amap_static_map(None, "北京")
        check("无 key 时 amap_static_map 给配置提示", "还没有配置" in out, out[:120])
        out = await p2.amap_weather(None, "北京")
        check("无 key 时 amap_weather 给配置提示", "还没有配置" in out, out[:120])

        await plugin.terminate()
        check("插件能正常关闭", plugin.server is None)


# ======================================================================
# 主流程
# ======================================================================
async def run_all() -> None:
    test_syntax()
    test_schema()
    test_coord()
    test_formatter()
    test_store()

    runner = await start_fake_amap()
    try:
        await test_client()
        await test_locate_loop()
        await test_end_to_end()
    finally:
        await runner.cleanup()

    section("汇总")
    failed = [x for x in RESULTS if not x[1]]
    print("共 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for name, _ok, detail in failed:
        print("  FAIL " + name + "  " + detail)
    if not failed:
        print("全部通过 ✔")


def main() -> int:
    bootstrap()
    try:
        asyncio.run(run_all())
    except Exception:
        traceback.print_exc()
        return 1
    return 1 if any(not x[1] for x in RESULTS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
