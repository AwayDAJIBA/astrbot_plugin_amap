"""按需定位：主机下发请求 → 手机取一次 GPS → 回传坐标。

与 phone_link 的常驻轮询不同，这里的定位是**拉取式**的：
只有 agent（或用户）真的需要位置时，插件才生成一个定位请求，
手机 App 下次轮询 /amap/poll 才会知道「该取位置了」，取完立刻上报。

这样手机上不需要常驻定位、不需要后台持续耗电。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from astrbot.api import logger

from .formatter import format_age


class LocateManager:
    def __init__(self, cfg, store, client=None) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client

    # ------------------------------------------------------------ 发起定位
    async def locate(
        self,
        device_id: str,
        reason: str = "agent 请求定位",
        timeout: int = 0,
        force: bool = False,
        accept_cache: bool = True,
    ) -> dict[str, Any]:
        """要一次手机位置。

        返回 dict：
            ok       成功与否
            from     cache / fresh
            location 位置记录
            error    失败原因
        """
        dev_id, err = self.store.resolve_device(device_id)
        if err:
            return {"ok": False, "error": err, "device_id": "", "from": "none"}

        # 1) 缓存够新就直接用，不去骚扰手机
        if accept_cache and not force:
            age = self.store.location_age(dev_id)
            max_age = float(self.cfg.locate_max_age)
            if age is not None and max_age > 0 and age <= max_age:
                return {
                    "ok": True,
                    "from": "cache",
                    "device_id": dev_id,
                    "location": self.store.last_location(dev_id),
                    "age": age,
                }

        # 2) 设备根本没在轮询的话，等下去也没意义，直接给个明白话
        if not self.store.device_exists(dev_id):
            return {"ok": False, "device_id": dev_id, "from": "none", "error": "设备从没连接过主机。"}
        if not self.store.is_online(dev_id):
            dev = self.store.get_device(dev_id) or {}
            offline = float(dev.get("offline_seconds") or 0)
            return {
                "ok": False,
                "device_id": dev_id,
                "from": "none",
                "error": (
                    "手机 " + dev_id + " 已经 " + format_age(offline)
                    + "没有轮询主机了（App 被系统杀了 / 断网 / 服务没开），这次定位没意义，先去把 App 的前台服务打开。"
                ),
            }

        wait_sec = int(timeout) if timeout else int(self.cfg.locate_timeout)
        wait_sec = max(5, min(300, wait_sec))

        request_id = "loc-" + uuid.uuid4().hex[:12]
        event = asyncio.Event()
        pending = {
            "id": request_id,
            "device_id": dev_id,
            "reason": str(reason or "agent 请求定位"),
            "created": time.time(),
            "ttl": wait_sec + 10,
            "timeout_ms": wait_sec * 1000,
            "max_age_ms": 15000,
            "high_accuracy": bool(self.cfg.high_accuracy),
            "event": event,
            "result": None,
            "dispatched": 0,
        }
        self.store.add_pending(request_id, pending)
        logger.info(
            "amap: 已向 " + dev_id + " 下发定位请求 " + request_id + "，最多等 " + str(wait_sec) + " 秒"
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=wait_sec)
        except asyncio.TimeoutError:
            self.store.drop_pending(request_id)
            hint = (
                "手机 " + dev_id + " 没有在 " + str(wait_sec) + " 秒内回传位置。"
                "常见原因：App 前台服务没开 / 被系统省电策略杀了 / 手机没授权定位权限 / 屏幕锁死时被限制。"
            )
            dev = self.store.get_device(dev_id) or {}
            hint += "（该设备最近一次轮询：" + format_age(time.time() - float(dev.get("last_seen") or 0)) + "）"
            return {"ok": False, "device_id": dev_id, "from": "none", "error": hint, "request_id": request_id}

        # pending 与 store 内部是同一个 dict 对象，resolve_pending 写的就是它
        result = dict(pending.get("result") or {})
        self.store.drop_pending(request_id)
        if str(result.get("error") or "").strip():
            return {
                "ok": False,
                "device_id": dev_id,
                "from": "fresh",
                "error": "手机取位置失败：" + str(result.get("error")),
                "location": result,
                "request_id": request_id,
            }
        return {
            "ok": True,
            "from": "fresh",
            "device_id": dev_id,
            "location": result,
            "age": 0.0,
            "request_id": request_id,
        }

    # ------------------------------------------------------------ 手机侧轮询
    def poll_for_device(self, device_id: str) -> dict[str, Any] | None:
        """手机轮询：有没有要我去取位置的请求？返回请求体或 None。"""
        dev_id = str(device_id or "").strip()
        if not dev_id:
            return None
        self.store.purge_expired()
        row = self.store.peek_pending_for(dev_id)
        if not row:
            return None
        # 同一条请求最多派发 3 次，防止手机反复取 GPS 却不回传
        if not self.store.mark_dispatched(str(row.get("id")), max_dispatch=3):
            return None
        return {
            "id": str(row.get("id")),
            "reason": str(row.get("reason") or ""),
            "timeout_ms": int(row.get("timeout_ms") or 45000),
            "max_age_ms": int(row.get("max_age_ms") or 15000),
            "high_accuracy": bool(row.get("high_accuracy", True)),
        }

    # ------------------------------------------------------------ 手机侧上报
    def handle_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        """手机上报坐标。带 request_id 就是应答某次请求，否则算主动上报。"""
        if not isinstance(payload, dict):
            payload = {}
        device_id = str(payload.get("device_id") or "").strip() or "unknown"
        self.store.touch_device(
            device_id,
            name=str(payload.get("name") or ""),
            remote_ip=str(payload.get("remote_ip") or ""),
            note="report",
            app_version=str(payload.get("app_version") or ""),
        )
        record = self.store.save_location(device_id, payload)
        request_id = str(payload.get("request_id") or "").strip()
        matched = False
        if request_id:
            matched = self.store.resolve_pending(request_id, record)
            if not matched:
                logger.info("amap: 收到 " + device_id + " 的过期/未知请求回包 " + request_id)
        logger.info(
            "amap: 收到位置 device=" + device_id
            + " request=" + (request_id or "-")
            + " error=" + (str(record.get("error")) or "-")
        )
        return {
            "device_id": device_id,
            "request_id": request_id,
            "matched": matched,
            "location": record,
        }

    # ------------------------------------------------------------ 辅助
    def last(self, device_id: str) -> dict[str, Any]:
        dev_id, err = self.store.resolve_device(device_id)
        if err:
            return {"ok": False, "error": err}
        rec = self.store.last_location(dev_id)
        if not rec:
            return {"ok": False, "device_id": dev_id, "error": "这台设备还没有任何位置记录。"}
        return {
            "ok": True,
            "device_id": dev_id,
            "age": self.store.location_age(dev_id),
            "location": rec,
        }
