"""本机网络地址探测。

用途：手机 App 里要填「主机地址」，但一台 Windows 机子上往往有好几块网卡
（WiFi 内网、有线、虚拟机、VPN/组网网卡……）。如果装了 Tailscale / ZeroTier
这类组网客户端，真正该填的往往不是 192.168.x.x，而是 100.x.x.x 那种虚拟地址。

所以插件在启动和 /amap_status 时把候选地址都列出来并标注用途，
省得用户挨个试。
"""
from __future__ import annotations

import ipaddress
import socket

__all__ = ["local_addresses", "describe_addresses", "classify"]

# 组网工具的常见虚拟网段（顺序很重要：CGNAT 段要最先判）
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def classify(ip: str) -> str:
    """给一个 IPv4 地址打上人话标签。"""
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return "未知"
    if addr.is_loopback:
        return "本机回环（手机填这个连不上）"
    if addr.is_multicast:
        return "组播地址"
    if addr.is_link_local:
        return "链路本地（一般不用）"
    # 必须先判这个：Tailscale 等组网工具落在 100.64.0.0/10，
    # 而 ipaddress 会把它同时算成 private，放到后面就会被误标成「保留/内网」。
    if addr in _TAILSCALE_NET:
        return "组网虚拟地址（Tailscale，出门就填它）"
    if ip.startswith("172.16.") or ip.startswith("172.17."):
        # 蒲公英（Oray）默认就发这个段，而且它同时也属于私有地址范围
        return "组网虚拟地址（蒲公英常用这个段，出门就填它）"
    if ip.startswith("192.168.") or ip.startswith("10."):
        return "内网地址（同一 WiFi 直接可用；组网工具也可能分这个段）"
    try:
        if addr in ipaddress.ip_network("172.16.0.0/12"):
            return "内网地址（可能是组网工具，也可能只是路由器内网）"
    except Exception:
        pass
    # Radmin VPN 之类的虚拟网卡常用 26.x / 25.x，看着像公网其实不是
    if ip.startswith("26.") or ip.startswith("25."):
        return "疑似虚拟网卡/VPN 地址（别急着当公网填）"
    if addr.is_global:
        return "公网地址"
    return "其他内网地址"


def local_addresses(include_ipv6: bool = False) -> list[str]:
    """列出本机所有网卡的 IP（去重、IPv4 优先）。"""
    found: list[str] = []

    def push(ip: str) -> None:
        ip = str(ip or "").strip()
        if not ip or ip in found:
            return
        if not include_ipv6 and ":" in ip:
            return
        if ip.startswith("127."):
            return  # 回环没有参考价值，别占位置
        found.append(ip)

    # 1) 用主机名解析（能拿到大部分网卡）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            push(info[4][0])
    except Exception:
        pass

    # 2) 兜底：UDP connect 拿到默认出口 IP（UDP connect 是本地操作，不会真的发包）
    for probe in ("8.8.8.8", "223.5.5.5", "1.1.1.1"):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect((probe, 80))
                push(sock.getsockname()[0])
            finally:
                sock.close()
            break
        except Exception:
            continue

    return found


def describe_addresses(port: int, include_ipv6: bool = False) -> str:
    """给 /amap_status 用的一段说明文本。"""
    addrs = local_addresses(include_ipv6=include_ipv6)
    if not addrs:
        return "（没能探测到本机地址，用 ipconfig 自己看一下）"

    def rank(ip: str) -> int:
        tag = classify(ip)
        if tag.startswith("组网"):
            return 0  # 出门靠它，排最前
        if tag.startswith("内网"):
            return 1
        if tag.startswith("疑似虚拟"):
            return 3
        return 2

    addrs.sort(key=lambda x: (rank(x), x))
    lines = []
    for ip in addrs:
        lines.append("  · http://" + ip + ":" + str(port) + "   " + classify(ip))

    vpn_count = sum(1 for x in addrs if classify(x).startswith("组网"))
    if vpn_count >= 2:
        lines.append("  （上面这些组网/内网地址里，选你真正装的那个组网工具分到的那个）")
    vm_count = sum(1 for x in addrs if x.startswith("192.168.") and x.endswith(".1"))
    if vm_count >= 1:
        lines.append(
            "  （192.168.x.1 这类 .1 结尾的多半是 VMware、VirtualBox 的虚拟网卡，手机填了连不上）"
        )
    if vpn_count == 0:
        lines.append(
            "  （没发现组网虚拟地址。要出门也能用，先在主机和手机上装好 Tailscale / 蒲公英 / ZeroTier "
            "并加进同一个网络，再回来看这一行）"
        )
    return "\n".join(lines)

