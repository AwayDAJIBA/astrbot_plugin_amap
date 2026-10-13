"""局域网 HTTP 服务（aiohttp）：手机 App <-> 插件。

接口（手机端契约，App 按这个写）：
  GET  /amap/ping?device_id=&name=     存活探测 + 设备心跳（返回版本/时间/建议轮询间隔）
  GET  /amap/poll?device_id=           手机轮询：有没有让我去取位置的请求
  POST /amap/report                    手机上报坐标（带 request_id 就是应答某次请求）
  GET  /amap/last?device_id=           最近一次已知位置（不触发手机）
  GET  /amap/history?device_id=&limit= 位置历史
  GET  /amap/health                    服务自检（含高德 key 是否已配）
  GET  /                               一个极简说明页，浏览器打开能确认连通

安全：仅局域网直连、明文 HTTP（和 phone_link 同一套取舍）。
      auth_token 非空时要求 Header X-Amap-Token 或 ?token= 匹配。
"""
from __future__ import annotations

import time

from aiohttp import web
from astrbot.api import logger

SERVER_NAME = "astrbot_plugin_amap"
VERSION = "1.0.0"


class AmapHttpServer:
    def __init__(self, cfg, store, locator, client=None) -> None:
        self.cfg = cfg
        self.store = store
        self.locator = locator
        self.client = client
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    # ------------------------------------------------------------ 工具
    def _authorized(self, request) -> bool:
        token = str(self.cfg.auth_token or "").strip()
        if not token:
            return True
        got = request.headers.get("X-Amap-Token") or request.query.get("token") or ""
        return str(got).strip() == token

    def _deny(self):
        return web.json_response({"ok": False, "error": "bad token"}, status=401)

    def _remote_ip(self, request) -> str:
        return request.remote or ""

    def _base_url(self, request) -> str:
        host = request.host or (str(self.cfg.listen_host) + ":" + str(self.cfg.listen_port))
        return "http://" + str(host)

    async def _body(self, request) -> dict:
        try:
            data = await request.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            # 兜底：有些客户端会以表单形式提交
            try:
                form = await request.post()
                data = {k: v for k, v in form.items()}
            except Exception:
                data = {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 接口
    async def handle_root(self, request):
        text = (
            SERVER_NAME + " " + VERSION + "\n"
            "这是 AstrBot 的高德地图插件，手机 App 请连以下接口：\n"
            "  GET  /amap/ping   存活探测\n"
            "  GET  /amap/poll   取定位请求\n"
            "  POST /amap/report 上报坐标\n"
            "  GET  /amap/last   最近位置\n"
            "  GET  /amap/health 服务自检\n"
        )
        return web.Response(text=text, content_type="text/plain", charset="utf-8")

    async def handle_ping(self, request):
        if not self._authorized(request):
            return self._deny()
        device_id = (request.query.get("device_id") or "").strip()
        name = (request.query.get("name") or "").strip()
        app_version = (request.query.get("app_version") or "").strip()
        if device_id:
            self.store.touch_device(
                device_id,
                name=name,
                remote_ip=self._remote_ip(request),
                note="ping",
                base_url=self._base_url(request),
                app_version=app_version,
            )
        return web.json_response(
            {
                "ok": True,
                "name": SERVER_NAME,
                "version": VERSION,
                "now": time.time(),
                "poll_interval": int(self.cfg.poll_interval),
                "device_known": bool(device_id) and self.store.device_exists(device_id),
                "locate_ready": True,
                "amap_key_configured": bool(self.cfg.has_key),
                "pending_requests": int(self.store.pending_count()),
            }
        )

    async def handle_poll(self, request):
        if not self._authorized(request):
            return self._deny()
        device_id = (request.query.get("device_id") or "").strip()
        if not device_id:
            return web.json_response({"ok": False, "error": "device_id required"}, status=400)
        self.store.touch_device(
            device_id,
            name=(request.query.get("name") or "").strip(),
            remote_ip=self._remote_ip(request),
            note="poll",
            base_url=self._base_url(request),
            app_version=(request.query.get("app_version") or "").strip(),
        )
        pending = self.locator.poll_for_device(device_id) if self.locator is not None else None
        return web.json_response({"ok": True, "now": time.time(), "request": pending})

    async def handle_report(self, request):
        if not self._authorized(request):
            return self._deny()
        body = await self._body(request)
        if not body:
            return web.json_response({"ok": False, "error": "empty body"}, status=400)
        body["remote_ip"] = self._remote_ip(request)
        device_id = str(body.get("device_id") or "").strip() or "unknown"
        # 坐标合法性粗校验（有 error 字段时允许不带坐标）
        if not str(body.get("error") or "").strip():
            try:
                lng = float(body.get("lng"))
                lat = float(body.get("lat"))
            except Exception:
                return web.json_response({"ok": False, "error": "lng/lat must be numbers"}, status=400)
            if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
                return web.json_response({"ok": False, "error": "lng/lat out of range"}, status=400)
        result = self.locator.handle_report(body)
        self.store.maybe_flush()
        return web.json_response(
            {
                "ok": True,
                "accepted": True,
                "device_id": device_id,
                "request_id": result.get("request_id") or "",
                "matched": bool(result.get("matched")),
            }
        )

    async def handle_last(self, request):
        if not self._authorized(request):
            return self._deny()
        device_id = (request.query.get("device_id") or "").strip()
        data = self.locator.last(device_id)
        return web.json_response(data)

    async def handle_history(self, request):
        if not self._authorized(request):
            return self._deny()
        device_id = (request.query.get("device_id") or "").strip()
        try:
            limit = int(request.query.get("limit") or 20)
        except Exception:
            limit = 20
        dev_id, err = self.store.resolve_device(device_id)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)
        return web.json_response(
            {"ok": True, "device_id": dev_id, "history": self.store.list_history(dev_id, limit)}
        )

    async def handle_health(self, request):
        if not self._authorized(request):
            return self._deny()
        self.store.purge_expired()
        return web.json_response(
            {
                "ok": True,
                "name": SERVER_NAME,
                "version": VERSION,
                "now": time.time(),
                "devices": len(self.store.list_devices()),
                "pending_requests": int(self.store.pending_count()),
                "amap_key_configured": bool(self.cfg.has_key),
                "api_calls": int(self.client.stats.get("calls", 0)) if self.client is not None else 0,
            }
        )

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/", self.handle_root)
        self.app.router.add_get("/amap/ping", self.handle_ping)
        self.app.router.add_get("/amap/poll", self.handle_poll)
        self.app.router.add_post("/amap/report", self.handle_report)
        self.app.router.add_get("/amap/last", self.handle_last)
        self.app.router.add_get("/amap/history", self.handle_history)
        self.app.router.add_get("/amap/health", self.handle_health)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.cfg.listen_host, int(self.cfg.listen_port))
        await self.site.start()
        logger.info(
            "amap: HTTP 服务已启动 http://"
            + str(self.cfg.listen_host)
            + ":"
            + str(self.cfg.listen_port)
            + "（手机请用主机内网 IP 访问）"
        )

    async def stop(self) -> None:
        try:
            if self.site is not None:
                await self.site.stop()
        except Exception as exc:
            logger.warning("amap: site.stop 出错: " + repr(exc))
        try:
            if self.runner is not None:
                await self.runner.cleanup()
        except Exception as exc:
            logger.warning("amap: runner.cleanup 出错: " + repr(exc))
        self.site = None
        self.runner = None
        self.app = None
