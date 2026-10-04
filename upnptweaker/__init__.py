"""纯标准库实现的 UPnP IGD 客户端(SSDP 发现 + SOAP 控制)。

用于在家里路由器上查询/添加/删除端口映射(NAT 端口转发)。
不依赖任何第三方包。
"""

from .ssdp import discover, SSDPDevice
from .device import Device, Service, DeviceError
from .soap import SOAPError, call as soap_call
from .igd import IGD, PortMapping, IGDError

__all__ = [
    "discover",
    "SSDPDevice",
    "Device",
    "Service",
    "DeviceError",
    "SOAPError",
    "soap_call",
    "IGD",
    "PortMapping",
    "IGDError",
]

__version__ = "0.1"
