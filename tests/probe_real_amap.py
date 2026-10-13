"""拿真高德服务器验证一次：用故意错误的 key，看插件能不能优雅识别。

只发 1 次请求（高德明令禁止压测），不消耗任何正常配额。
"""
import asyncio
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ASTRBOT_ROOT = PLUGIN_DIR.parent.parent.parent
sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

import astrbot  # noqa: F401,E402

pkg = types.ModuleType("astrbot_plugin_amap")
pkg.__path__ = [str(PLUGIN_DIR)]
pkg.__package__ = "astrbot_plugin_amap"
sys.modules["astrbot_plugin_amap"] = pkg

from astrbot_plugin_amap.amap.client import AmapClient, AmapError  # noqa: E402
from astrbot_plugin_amap.amap.config import AmapConfig  # noqa: E402


async def main() -> int:
    cfg = AmapConfig.from_dict({
        "amap_key": "00000000000000000000000000000000",  # 故意错
        "request_timeout": 12,
        "retry_times": 0,
        "cache_ttl": 0,
    })
    client = AmapClient(cfg)
    try:
        print("→ 请求真实 https://restapi.amap.com/v3/geocode/geo （用假 key）")
        await client.geocode("北京市朝阳区阜通东大街6号")
        print("！！ 假 key 居然成功了？")
        return 1
    except AmapError as exc:
        print("   code     =", exc.code)
        print("   path     =", exc.path)
        print("   human    =", exc.human())
        ok = exc.code in ("10001", "10009", "10013", "10015", "20000", "20001", "20003")
        print("   识别为「key 类错误」:", ok)
        return 0 if ok else 1
    except Exception as exc:
        print("！！ 非 AmapError：", repr(exc))
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
