"""下载并解析 UPnP 设备描述 XML,找出 WAN 连接服务。"""

from __future__ import annotations

import http.client
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .soap import _local

DESCRIPTION_TIMEOUT = 8.0

# 能提供端口映射能力的服务类型关键字。
# 注意:WANCommonInterfaceConfig 虽然也叫 WAN 服务,但它只报链路状态,
# 不支持 AddPortMapping,所以不算在内。
CONNECTION_SERVICE_HINTS = ("wanipconnection", "wanpppconnection")


class DeviceError(Exception):
    pass


@dataclass
class Service:
    service_type: str
    service_id: str
    control_url: str          # 绝对地址
    event_sub_url: str = ""
    scpd_url: str = ""
    parent_device: str = ""   # 所属设备名,方便多 WAN 时区分
    parent_udn: str = ""

    @property
    def short_type(self) -> str:
        """urn:schemas-upnp-org:service:WANIPConnection:1 -> WANIPConnection:1"""
        parts = self.service_type.split(":")
        return ":".join(parts[-2:]) if len(parts) >= 2 else self.service_type

    @property
    def is_connection_service(self) -> bool:
        low = self.service_type.lower()
        return any(h in low for h in CONNECTION_SERVICE_HINTS)

    @property
    def is_ppp(self) -> bool:
        return "wanpppconnection" in self.service_type.lower()

    def __str__(self) -> str:
        label = f"{self.parent_device} — {self.short_type}" if self.parent_device else self.short_type
        return label


@dataclass
class Device:
    location: str
    friendly_name: str = ""
    manufacturer: str = ""
    model_name: str = ""
    model_number: str = ""
    udn: str = ""
    device_type: str = ""
    services: list[Service] = field(default_factory=list)

    @property
    def connection_services(self) -> list[Service]:
        return [s for s in self.services if s.is_connection_service]

    def __str__(self) -> str:
        bits = [self.friendly_name or "未知设备"]
        if self.model_name:
            bits.append(f"[{self.model_name}]")
        host = urlparse(self.location).hostname or ""
        if host:
            bits.append(host)
        return " ".join(bits)


def _fetch_xml(url: str, timeout: float = DESCRIPTION_TIMEOUT) -> bytes:
    u = urlparse(url)
    if not u.hostname:
        raise DeviceError(f"描述地址不合法: {url!r}")
    path = u.path or "/"
    if u.query:
        path = f"{path}?{u.query}"

    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.request(
            "GET",
            path,
            headers={
                "User-Agent": "upnptweaker/1.0 UPnP/1.0",
                "Accept": "text/xml, application/xml, */*",
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise DeviceError(f"获取设备描述失败:HTTP {resp.status} ({url})")
        return data
    except OSError as exc:
        raise DeviceError(f"获取设备描述失败:无法连接 {u.hostname} ({exc})") from exc
    finally:
        conn.close()


def _text(elem: ET.Element | None, name: str) -> str:
    """在直接子节点里找名为 name 的节点并取文本。"""
    if elem is None:
        return ""
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _walk_devices(node: ET.Element, location: str, out: list[Service], depth: int = 0) -> None:
    """递归遍历 device/deviceList,收集所有服务。"""
    if depth > 8:
        return

    friendly = _text(node, "friendlyName")
    udn = _text(node, "UDN")
    if not friendly and not udn:
        return

    for service_list in node:
        if _local(service_list.tag) != "serviceList":
            continue
        for svc in service_list:
            if _local(svc.tag) != "service":
                continue
            stype = _text(svc, "serviceType")
            control = _text(svc, "controlURL")
            if not stype or not control:
                continue
            out.append(
                Service(
                    service_type=stype,
                    service_id=_text(svc, "serviceId"),
                    control_url=urljoin(location, control),
                    event_sub_url=urljoin(location, _text(svc, "eventSubURL")),
                    scpd_url=urljoin(location, _text(svc, "SCPDURL")),
                    parent_device=friendly,
                    parent_udn=udn,
                )
            )

    for child_list in node:
        if _local(child_list.tag) != "deviceList":
            continue
        for sub in child_list:
            if _local(sub.tag) == "device":
                _walk_devices(sub, location, out, depth + 1)


def fetch_device(location: str, timeout: float = DESCRIPTION_TIMEOUT) -> Device:
    """下载并解析一份设备描述 XML。"""
    data = _fetch_xml(location, timeout)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DeviceError(f"设备描述 XML 解析失败: {exc}") from exc

    # 找到根 device 节点
    root_dev = None
    for elem in root.iter():
        if _local(elem.tag) == "device":
            root_dev = elem
            break
    if root_dev is None:
        raise DeviceError("设备描述里没有 <device> 节点")

    services: list[Service] = []
    _walk_devices(root_dev, location, services)

    return Device(
        location=location,
        friendly_name=_text(root_dev, "friendlyName"),
        manufacturer=_text(root_dev, "manufacturer"),
        model_name=_text(root_dev, "modelName"),
        model_number=_text(root_dev, "modelNumber"),
        udn=_text(root_dev, "UDN"),
        device_type=_text(root_dev, "deviceType"),
        services=services,
    )
