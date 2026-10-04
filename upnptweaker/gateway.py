"""把发现、设备描述解析、服务挑选串起来,直接给出可用的 IGD 列表。"""

from __future__ import annotations

from dataclasses import dataclass

from .device import Device, DeviceError, fetch_device
from .igd import IGD, IGDError
from .ssdp import discover


@dataclass
class Gateway:
    """一个已就绪、可以直接操作的 WAN 连接服务。"""

    device: Device
    service: object  # Service
    igd: IGD
    location: str

    @property
    def label(self) -> str:
        name = self.device.friendly_name or self.device.model_name or "未知路由器"
        return f"{name} — {self.service.short_type}"

    @property
    def display(self) -> str:
        name = self.device.friendly_name or "未知路由器"
        model = f" ({self.device.model_name})" if self.device.model_name else ""
        return f"{name}{model} — {self.service.short_type}"


def find_gateways(timeout: float = 2.5, on_status=None, timeout_soap: float = 10.0) -> list[Gateway]:
    """搜索局域网内所有可用的 WAN 连接服务。

    失败和不可用的候选会被跳过,不会中断整体流程;最后若一个都没有,
    抛 IGDError 并带上排查提示。
    """
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    found = discover(timeout=timeout, on_status=on_status)
    if not found:
        raise IGDError(
            "没有发现任何 UPnP 设备。请检查:\n"
            "  · 路由器管理页面里 UPnP 开关是否已打开\n"
            "  · 电脑是否和路由器在同一网段\n"
            "  · Windows 防火墙是否拦截了 SSDP(1900/UDP)\n"
            "  · 是否开了 VPN(会改变默认路由)"
        )

    gateways: list[Gateway] = []
    seen_locations: set[str] = set()

    for ssdp_dev in found:
        if not ssdp_dev.is_igateway:
            continue
        if ssdp_dev.location in seen_locations:
            continue
        seen_locations.add(ssdp_dev.location)

        try:
            status(f"读取设备描述:{ssdp_dev.location}")
            device = fetch_device(ssdp_dev.location)
        except DeviceError as exc:
            status(f"跳过 {ssdp_dev.location}:{exc}")
            continue

        services = device.connection_services
        if not services:
            status(f"{device.friendly_name or ssdp_dev.location} 没有 WAN 连接服务,跳过")
            continue

        # 优先 WANIPConnection;PPP 连接排在后面
        services.sort(key=lambda s: (s.is_ppp, s.service_type))

        for svc in services:
            try:
                igd = IGD(device, svc, timeout=timeout_soap)
            except IGDError:
                continue
            gateways.append(Gateway(device=device, service=svc, igd=igd, location=ssdp_dev.location))

    if not gateways:
        raise IGDError(
            "发现了 UPnP 设备,但都没有提供端口映射服务(WANIPConnection/WANPPPConnection)。\n"
            "常见原因:路由器把 UPnP 关了、或者当前是二级路由/运营商光猫在拨号。"
        )

    status(f"就绪:{len(gateways)} 个可操作的 WAN 服务。")
    return gateways
