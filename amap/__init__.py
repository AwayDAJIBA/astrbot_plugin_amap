"""astrbot_plugin_amap 的内部包。

模块分工:
    config.py    配置解析
    coord.py     坐标系统转换（WGS-84 / GCJ-02 / BD-09）
    netinfo.py   本机网卡地址探测（告诉用户手机该填哪个地址）
    client.py    高德 Web 服务异步客户端
    formatter.py 高德响应 → 喂给 LLM 的短文本
    store.py     设备表 + 位置仓储（可落盘）
    locator.py   按需定位（下发请求 → 手机取 GPS → 回传）
    server.py    局域网 HTTP 服务（手机 App 的对接面）
"""

from . import netinfo
from .client import AmapClient, AmapError
from .config import AmapConfig
from .locator import LocateManager
from .server import AmapHttpServer
from .store import LocationStore

__all__ = [
    "AmapClient",
    "AmapError",
    "AmapConfig",
    "LocateManager",
    "AmapHttpServer",
    "LocationStore",
    "netinfo",
]
