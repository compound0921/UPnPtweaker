"""SSDP 发现:在局域网里找到 UPnP 网关设备(路由器)。

实现方式:向组播地址 239.255.255.250:1900 发送若干条 M-SEARCH 请求,
把返回的响应按 USN/LOCATION 去重后返回。
"""

from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass, field

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# 用来探出"默认路由走哪张网卡"的靶子。UDP connect() 不发包,只查路由表,
# 所以填什么公网地址都行;多列几个是为了不同地区的网络都能命中。
_ROUTE_PROBE_TARGETS = ("223.5.5.5", "8.8.8.8", "1.1.1.1")

# 依次搜索这些类型。前几个是"互联网网关设备"整体,
# 后几个直接搜 WAN 连接服务(部分路由器只响应对服务的搜索)。
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
    "ssdp:all",
)

_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: {addr}:{port}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: {mx}\r\n"
    "ST: {st}\r\n"
    "\r\n"
)

_STATUS_RE = re.compile(rb"^HTTP/1\.\d\s+(\d+)")
_HEADER_RE = re.compile(rb"^([A-Za-z0-9\-_]+)\s*:\s*(.*?)\s*$")


@dataclass
class SSDPDevice:
    """一条 SSDP 响应。"""

    location: str          # 设备描述 XML 的 URL
    st: str = ""           # 搜索目标 / 设备类型
    usn: str = ""          # 唯一序列号
    server: str = ""       # 服务端标识(通常含路由器型号)
    source_ip: str = ""    # 响应的来源 IP(即路由器的 LAN 地址)
    raw_headers: dict = field(default_factory=dict)

    @property
    def is_igateway(self) -> bool:
        s = self.st.lower()
        return "internetgatewaydevice" in s or "wanipconnection" in s or "wanpppconnection" in s

    def __str__(self) -> str:
        name = self.server or self.st or "未知设备"
        return f"{name}  ({self.source_ip})"


def _parse_response(data: bytes, source_ip: str) -> SSDPDevice | None:
    """解析一条 SSDP 响应报文,失败返回 None。"""
    text = data.decode("utf-8", "replace")
    lines = text.split("\r\n")
    if not lines or not _STATUS_RE.match(lines[0].encode("utf-8", "replace")):
        # 有些设备(或某些 HTTP 版本)用 \n 分行,退一步按 \n 切
        lines = text.split("\n")
        if not lines or not _STATUS_RE.match(lines[0].encode("utf-8", "replace")):
            return None

    m = _STATUS_RE.match(lines[0].encode("utf-8", "replace"))
    if m.group(1) != b"200":
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            break
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        headers[key.strip().upper()] = val.strip()

    location = headers.get("LOCATION") or headers.get("Location") or ""
    if not location:
        return None

    return SSDPDevice(
        location=location,
        st=headers.get("ST", ""),
        usn=headers.get("USN", ""),
        server=headers.get("SERVER", ""),
        source_ip=source_ip,
        raw_headers=headers,
    )


def _local_ipv4_addresses() -> list[str]:
    """列出本机所有可用的 IPv4 地址(用于逐网卡发送)。"""
    addrs: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addrs and not ip.startswith("127."):
                addrs.append(ip)
    except OSError:
        pass

    # 通过默认路由探测出出口网卡的地址,它最可能是连着路由器的那张。
    # UDP connect() 不实际发包,只要有路由到这个地址就行 ——
    # 所以随便一个可路由的公网地址都可以,列几个是为了覆盖不同地区。
    for target in _ROUTE_PROBE_TARGETS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((target, 53))
                ip = s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            continue
        if ip and not ip.startswith("127.") and ip not in addrs:
            addrs.insert(0, ip)
        break

    return addrs


def local_ip_toward(host: str) -> str:
    """返回本机用于访问 host 的那个地址(即出口网卡的 IP)。"""
    if not host:
        return ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 1900))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


def _search_once(
    targets: tuple[str, ...],
    timeout: float,
    source_ip: str | None = None,
) -> list[SSDPDevice]:
    """在单张网卡上发一轮 M-SEARCH 并收集响应。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if source_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source_ip))
            try:
                sock.bind((source_ip, 0))
            except OSError:
                pass

        for st in targets:
            msg = _MSEARCH.format(addr=SSDP_ADDR, port=SSDP_PORT, mx=max(1, int(timeout)), st=st)
            try:
                sock.sendto(msg.encode("ascii"), (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass

        deadline = time.monotonic() + timeout
        found: list[SSDPDevice] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                break
            dev = _parse_response(data, addr[0])
            if dev is not None:
                found.append(dev)
        return found
    finally:
        sock.close()


def _priority(dev: SSDPDevice) -> tuple[int, int]:
    return (0 if dev.is_igateway else 1, 0 if dev.location else 1)


def discover(
    timeout: float = 2.5,
    targets: tuple[str, ...] = SEARCH_TARGETS,
    on_status=None,
) -> list[SSDPDevice]:
    """搜索局域网内的 UPnP 设备。

    会在本机每张网卡上**同时**发一轮 M-SEARCH。多网卡 / 有 VPN 的机器上,
    走默认路由的那张网卡未必连着你家路由器,所以必须逐张试;并行做可以把
    总耗时压在单轮超时以内。
    """
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    status("正在搜索 UPnP 设备…")

    addresses = _local_ipv4_addresses()
    if not addresses:
        addresses = [None]  # type: ignore[list-item]

    results: list[list[SSDPDevice]] = [[] for _ in addresses]

    def probe(slot: int, source_ip: str | None) -> None:
        try:
            results[slot] = _search_once(targets, timeout, source_ip=source_ip)
        except Exception:  # noqa: BLE001 - 单张网卡失败不影响其它网卡
            results[slot] = []

    threads = [
        threading.Thread(target=probe, args=(i, ip), daemon=True)
        for i, ip in enumerate(addresses)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 3.0)

    raw = [dev for group in results for dev in group]
    if len(addresses) > 1:
        status(f"已在 {len(addresses)} 张网卡上搜索。")

    if not raw:
        status("没有收到任何 SSDP 响应。")
        return []

    # 去重:同一个 USN + LOCATION 只保留一条;保留信息最全的那条
    unique: dict[tuple[str, str], SSDPDevice] = {}
    for dev in raw:
        key = (dev.usn or dev.location, dev.location)
        old = unique.get(key)
        if old is None or _priority(dev) < _priority(old):
            unique[key] = dev

    gateways = [d for d in unique.values() if d.is_igateway]
    result = sorted(unique.values(), key=lambda d: (not d.is_igateway, d.st))
    status(f"发现 {len(result)} 个 UPnP 设备,其中 {len(gateways)} 个网关。")
    return result
