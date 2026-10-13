"""设备表 + 位置仓储（内存为主，可选落盘）。

比 phone_link 的最小版多做两件事：
1. 位置历史会落盘（重启不丢），因为「上次在哪」对 agent 很有用；
2. 定位请求的等待者用 asyncio.Event 唤醒，`locate_phone` 工具能同步等到结果。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import coord

STATE_VERSION = 1


def now_ts() -> float:
    return time.time()


class LocationStore:
    def __init__(self, data_dir: Path, cfg) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.devices: dict[str, dict[str, Any]] = {}
        self.locations: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._lock = threading.RLock()
        self._last_flush = 0.0
        self._flush_interval = 3.0
        if cfg.persist:
            self.load()

    # ------------------------------------------------------------ 设备
    def touch_device(
        self,
        device_id: str,
        name: str = "",
        remote_ip: str = "",
        note: str = "",
        base_url: str = "",
        app_version: str = "",
    ) -> dict[str, Any]:
        dev_id = str(device_id or "").strip()
        if not dev_id:
            return {}
        with self._lock:
            row = self.devices.get(dev_id)
            if row is None:
                row = {
                    "device_id": dev_id,
                    "name": "",
                    "first_seen": now_ts(),
                    "remote_ip": "",
                    "note": "",
                    "base_url": "",
                    "app_version": "",
                    "polls": 0,
                }
                self.devices[dev_id] = row
            if name:
                row["name"] = str(name)
            if remote_ip:
                row["remote_ip"] = str(remote_ip)
            if note:
                row["note"] = str(note)
            if base_url:
                row["base_url"] = str(base_url)
            if app_version:
                row["app_version"] = str(app_version)
            row["last_seen"] = now_ts()
            row["polls"] = int(row.get("polls", 0)) + 1
            self._dirty = True
        self.maybe_flush()
        return row

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.devices.get(str(device_id or "").strip())
            return dict(row) if row else None

    def device_exists(self, device_id: str) -> bool:
        with self._lock:
            return str(device_id or "").strip() in self.devices

    def is_online(self, device_id: str) -> bool:
        row = self.get_device(device_id)
        if not row:
            return False
        return (now_ts() - float(row.get("last_seen") or 0)) <= float(self.cfg.device_offline_sec)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(x) for x in self.devices.values()]
        out = []
        for row in rows:
            dev_id = str(row.get("device_id"))
            age = now_ts() - float(row.get("last_seen") or 0)
            row["online"] = age <= float(self.cfg.device_offline_sec)
            row["offline_seconds"] = round(age, 1)
            loc = self.locations.get(dev_id)
            if loc:
                row["location_age"] = round(now_ts() - float(loc.get("server_ts") or 0), 1)
            row["pending_requests"] = sum(1 for p in self._pending.values() if p.get("device_id") == dev_id)
            out.append(row)
        out.sort(key=lambda x: float(x.get("last_seen") or 0), reverse=True)
        return out

    def resolve_device(self, device_id: str = "") -> tuple[str, str]:
        """把用户/agent 给的 device_id 收敛成真实设备。

        返回 (device_id, 错误信息)。device_id 为空时用配置里的默认设备；
        只有一个设备时自动用它（省得 agent 每次都问用户设备号）。
        """
        want = str(device_id or "").strip()
        if want:
            if self.device_exists(want):
                return want, ""
            rows = self.list_devices()
            return "", (
                "没见过设备 " + want + "。已知设备："
                + (", ".join(str(r.get("device_id")) for r in rows) if rows else "（一个都没有）")
            )
        default = str(self.cfg.default_device or "").strip()
        if default and self.device_exists(default):
            return default, ""
        rows = self.list_devices()
        if len(rows) == 1:
            return str(rows[0].get("device_id")), ""
        if not rows:
            return "", "目前没有任何手机连过主机（确认手机 App 已启动、地址端口填对、同一个 WiFi）。"
        return "", (
            "有多台设备，必须指定 device_id：" + ", ".join(str(r.get("device_id")) for r in rows)
        )

    # ------------------------------------------------------------ 位置
    def save_location(self, device_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        """存一条位置上报表，同时更新最新位置与历史。"""
        dev_id = str(device_id or "").strip() or "unknown"
        entry = dict(rec)
        entry["device_id"] = dev_id
        entry["server_ts"] = now_ts()
        if not entry.get("ts"):
            entry["ts"] = entry["server_ts"]
        # 原始上报按 WGS-84 看待；同时算出 GCJ-02 给高德用
        err = str(entry.get("error") or "").strip()
        if not err:
            try:
                lng = float(entry.get("lng"))
                lat = float(entry.get("lat"))
                # 手机端定位失败时会填 0,0；那不是几内亚湾，是没拿到位置
                if abs(lng) < 1e-6 and abs(lat) < 1e-6:
                    raise ValueError("坐标为 0,0，视为定位失败")
                entry["wgs_lng"] = lng
                entry["wgs_lat"] = lat
                if bool(self.cfg.auto_convert):
                    glng, glat = coord.wgs84_to_gcj02(lng, lat)
                else:
                    glng, glat = lng, lat
                entry["gcj_lng"] = glng
                entry["gcj_lat"] = glat
            except Exception as exc:
                entry["error"] = "坐标字段缺失或非法（" + str(exc) + "）"
                err = entry["error"]
        with self._lock:
            if not err:
                self.locations[dev_id] = entry
                limit = int(self.cfg.history_limit)
                if limit > 0:
                    hist = self.history.setdefault(dev_id, [])
                    hist.append(entry)
                    if len(hist) > limit:
                        del hist[: len(hist) - limit]
            self._dirty = True
        self.flush()
        return entry

    def last_location(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self.locations.get(str(device_id or "").strip())
            return dict(rec) if rec else None

    def update_location_meta(self, device_id: str, meta: dict[str, Any]) -> None:
        """把逆地理解析出来的地址信息回写到最近一条位置记录上。

        locations 与 history 里存的是同一个 dict 对象，改一次两边都生效。
        """
        dev_id = str(device_id or "").strip()
        clean = {k: v for k, v in (meta or {}).items() if v}
        if not clean:
            return
        with self._lock:
            rec = self.locations.get(dev_id)
            if not rec:
                return
            rec.update(clean)
            self._dirty = True
        self.maybe_flush()

    def location_age(self, device_id: str) -> float | None:
        rec = self.last_location(device_id)
        if not rec:
            return None
        return now_ts() - float(rec.get("server_ts") or 0)

    def list_history(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            hist = list(self.history.get(str(device_id or "").strip(), []))
        if limit > 0:
            hist = hist[-limit:]
        return [dict(x) for x in hist]

    # ------------------------------------------------------------ 待处理定位请求
    def add_pending(self, request_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._pending[request_id] = payload

    def get_pending(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._pending.get(request_id)
            return dict(row) if row else None

    def peek_pending_for(self, device_id: str) -> dict[str, Any] | None:
        """取该设备最早的一个未过期请求（不移除，等手机确认收下了再移除）。"""
        dev_id = str(device_id or "").strip()
        with self._lock:
            rows = [p for p in self._pending.values() if p.get("device_id") == dev_id]
        rows.sort(key=lambda x: float(x.get("created") or 0))
        for row in rows:
            if now_ts() - float(row.get("created") or 0) <= float(row.get("ttl") or 60):
                return dict(row)
        return None

    def drop_pending(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(str(request_id or ""), None)

    def mark_dispatched(self, request_id: str, max_dispatch: int = 3) -> bool:
        """标记该请求已被派发给手机一次。超过次数上限返回 False（不再重复派发）。"""
        with self._lock:
            row = self._pending.get(str(request_id or ""))
            if row is None:
                return False
            if int(row.get("dispatched") or 0) >= int(max_dispatch):
                return False
            row["dispatched"] = int(row.get("dispatched") or 0) + 1
            row["dispatched_at"] = now_ts()
            return True

    def resolve_pending(self, request_id: str, result: dict[str, Any]) -> bool:
        """把位置结果写回等待中的请求并唤醒等待者。

        注意：get_pending() 返回的是浅拷贝副本，直接改副本不会生效，
        所以必须用这个方法在**原对象**上写。
        """
        with self._lock:
            row = self._pending.get(str(request_id or ""))
            if row is None:
                return False
            row["result"] = dict(result)
            event = row.get("event")
        if isinstance(event, asyncio.Event):
            try:
                event.set()
            except Exception as exc:  # pragma: no cover
                logger.warning("amap: 唤醒等待者失败: " + repr(exc))
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def purge_expired(self) -> int:
        """清掉过期请求，返回清掉的数量。"""
        removed = 0
        with self._lock:
            for rid in list(self._pending.keys()):
                row = self._pending.get(rid) or {}
                if now_ts() - float(row.get("created") or 0) > float(row.get("ttl") or 60):
                    self._pending.pop(rid, None)
                    removed += 1
        return removed

    # ------------------------------------------------------------ 落盘
    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": STATE_VERSION,
                "saved_at": now_ts(),
                "devices": self.devices,
                "locations": self.locations,
                "history": self.history,
            }

    def flush(self) -> None:
        if not self.cfg.persist:
            self._dirty = False
            return
        try:
            payload = json.dumps(self._snapshot(), ensure_ascii=False)
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.state_file)
            self._dirty = False
            self._last_flush = now_ts()
        except Exception as exc:
            logger.warning("amap: 写状态文件失败: " + repr(exc))

    def maybe_flush(self) -> None:
        if not self._dirty:
            return
        if now_ts() - self._last_flush < self._flush_interval:
            return
        self.flush()

    def load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("amap: 读状态文件失败（忽略）: " + repr(exc))
            return
        if not isinstance(data, dict):
            return
        devices = data.get("devices")
        locations = data.get("locations")
        history = data.get("history")
        with self._lock:
            if isinstance(devices, dict):
                self.devices = {str(k): v for k, v in devices.items() if isinstance(v, dict)}
            if isinstance(locations, dict):
                self.locations = {str(k): v for k, v in locations.items() if isinstance(v, dict)}
            if isinstance(history, dict):
                self.history = {
                    str(k): [x for x in v if isinstance(x, dict)]
                    for k, v in history.items()
                    if isinstance(v, list)
                }
        logger.info(
            "amap: 已载入状态文件 devices=" + str(len(self.devices))
            + " locations=" + str(len(self.locations))
        )
