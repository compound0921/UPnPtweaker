"""IGD 高层接口:把 SOAP 动作包装成好用的方法。

一个 IGD 实例绑定到某个设备上的一个 WAN 连接服务
(WANIPConnection 或 WANPPPConnection)。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from .device import Device, Service
from .soap import SOAPError, call

# 枚举端口映射时的安全上限,防止个别路由器在 713 之前一直回成功
MAX_MAPPING_SCAN = 512

PROTOCOLS = ("TCP", "UDP")


class IGDError(Exception):
    pass


@dataclass
class PortMapping:
    """一条端口映射规则。"""

    index: int = -1
    remote_host: str = ""
    external_port: int = 0
    protocol: str = "TCP"
    internal_port: int = 0
    internal_client: str = ""
    enabled: bool = True
    description: str = ""
    lease_duration: int = 0   # 秒;0 表示永久

    @property
    def lease_text(self) -> str:
        if self.lease_duration <= 0:
            return "永久"
        d, rem = divmod(self.lease_duration, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        bits = []
        if d:
            bits.append(f"{d}天")
        if h:
            bits.append(f"{h}小时")
        if m:
            bits.append(f"{m}分")
        if s and not bits:
            bits.append(f"{s}秒")
        return "".join(bits) or "0秒"

    def as_args(self) -> dict:
        return {
            "NewRemoteHost": self.remote_host,
            "NewExternalPort": str(self.external_port),
            "NewProtocol": self.protocol,
            "NewInternalPort": str(self.internal_port),
            "NewInternalClient": self.internal_client,
            "NewEnabled": "1" if self.enabled else "0",
            "NewPortMappingDescription": self.description,
            "NewLeaseDuration": str(self.lease_duration),
        }


def guess_local_ip(router_host: str) -> str:
    """猜本机在路由器所在网段的 IP —— 添加映射时的默认"内部客户端"。"""
    if router_host:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((router_host, 1900))
                return s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""


class IGD:
    """对某个 WAN 连接服务的操作封装。"""

    def __init__(self, device: Device, service: Service, timeout: float = 10.0) -> None:
        if not service.is_connection_service:
            raise IGDError(f"{service.service_type} 不支持端口映射")
        self.device = device
        self.service = service
        self.timeout = timeout

    # ---- 基础信息 ----

    def _call(self, action: str, args: dict | None = None) -> dict[str, str]:
        try:
            return call(
                self.service.control_url,
                self.service.service_type,
                action,
                args,
                timeout=self.timeout,
            )
        except SOAPError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一收口成 IGDError
            raise IGDError(f"{action} 调用失败:{exc}") from exc

    def get_external_ip(self) -> str:
        """查询路由器拿到的公网 IP。"""
        try:
            res = self._call("GetExternalIPAddress")
        except SOAPError as exc:
            raise IGDError(f"查询外网 IP 失败:{exc}") from exc
        ip = (res.get("NewExternalIPAddress") or "").strip()
        return ip

    def get_status_info(self) -> dict[str, str]:
        try:
            res = self._call("GetStatusInfo")
        except SOAPError as exc:
            raise IGDError(f"查询连接状态失败:{exc}") from exc
        return {
            "status": res.get("NewConnectionStatus", ""),
            "last_error": res.get("NewLastConnectionError", ""),
            "uptime": res.get("NewUptime", ""),
        }

    def get_connection_type_info(self) -> dict[str, str]:
        try:
            res = self._call("GetConnectionTypeInfo")
        except SOAPError as exc:
            raise IGDError(f"查询连接类型失败:{exc}") from exc
        return {
            "type": res.get("NewConnectionType", ""),
            "possible": res.get("NewPossibleConnectionTypes", ""),
        }

    # ---- 端口映射 ----

    def add_port_mapping(
        self,
        external_port: int,
        internal_port: int,
        internal_client: str,
        protocol: str = "TCP",
        description: str = "",
        remote_host: str = "",
        lease_duration: int = 0,
    ) -> None:
        """添加(或覆盖)一条端口映射。

        若该(外部端口, 协议)已存在,IGD 规范要求路由器覆盖它,
        但不少路由器会返回 718 冲突 —— 此时上层可先删再加。
        """
        protocol = protocol.upper()
        if protocol not in PROTOCOLS:
            raise IGDError(f"协议只能是 {PROTOCOLS[0]} 或 {PROTOCOLS[1]},收到 {protocol!r}")
        if not 1 <= int(external_port) <= 65535:
            raise IGDError(f"外部端口超出范围: {external_port}")
        if not 1 <= int(internal_port) <= 65535:
            raise IGDError(f"内部端口超出范围: {internal_port}")
        if int(lease_duration) < 0:
            raise IGDError("租期不能为负数(0 表示永久)")

        args = {
            "NewRemoteHost": remote_host,
            "NewExternalPort": str(int(external_port)),
            "NewProtocol": protocol,
            "NewInternalPort": str(int(internal_port)),
            "NewInternalClient": internal_client,
            "NewEnabled": "1",
            "NewPortMappingDescription": description[:80],
            "NewLeaseDuration": str(int(lease_duration)),
        }
        self._call("AddPortMapping", args)

    def delete_port_mapping(
        self,
        external_port: int,
        protocol: str = "TCP",
        remote_host: str = "",
    ) -> None:
        """删除一条端口映射。"""
        self._call(
            "DeletePortMapping",
            {
                "NewRemoteHost": remote_host,
                "NewExternalPort": str(int(external_port)),
                "NewProtocol": protocol.upper(),
            },
        )

    def get_specific_port_mapping(
        self,
        external_port: int,
        protocol: str = "TCP",
        remote_host: str = "",
    ) -> PortMapping | None:
        """查询单条映射;不存在返回 None。"""
        try:
            res = self._call(
                "GetSpecificPortMappingEntry",
                {
                    "NewRemoteHost": remote_host,
                    "NewExternalPort": str(int(external_port)),
                    "NewProtocol": protocol.upper(),
                },
            )
        except SOAPError as exc:
            if exc.code in (714, 713):
                return None
            raise IGDError(f"查询端口映射失败:{exc}") from exc
        return PortMapping(
            index=-1,
            remote_host=remote_host,
            external_port=int(external_port),
            protocol=protocol.upper(),
            internal_port=_int(res.get("NewInternalPort")),
            internal_client=res.get("NewInternalClient", ""),
            enabled=res.get("NewEnabled", "1") == "1",
            description=res.get("NewPortMappingDescription", ""),
            lease_duration=_int(res.get("NewLeaseDuration")),
        )

    def list_port_mappings(self, on_progress=None) -> list[PortMapping]:
        """枚举路由器上所有端口映射。

        靠 GetGenericPortMappingEntry 逐个索引读取,直到路由器返回 713
        (索引越界)—— 这是 UPnP 规范里唯一的枚举方式。
        """
        mappings: list[PortMapping] = []
        seen: set[tuple] = set()

        for idx in range(MAX_MAPPING_SCAN):
            try:
                res = self._call("GetGenericPortMappingEntry", {"NewPortMappingIndex": str(idx)})
            except SOAPError as exc:
                if exc.code == 713:
                    break  # 正常读完
                if exc.code == 714:
                    continue  # 中间有空洞,跳过继续
                raise IGDError(f"读取第 {idx} 条映射失败:{exc}") from exc

            m = PortMapping(
                index=idx,
                remote_host=res.get("NewRemoteHost", ""),
                external_port=_int(res.get("NewExternalPort")),
                protocol=res.get("NewProtocol", "TCP").upper(),
                internal_port=_int(res.get("NewInternalPort")),
                internal_client=res.get("NewInternalClient", ""),
                enabled=res.get("NewEnabled", "1") == "1",
                description=res.get("NewPortMappingDescription", ""),
                lease_duration=_int(res.get("NewLeaseDuration")),
            )

            key = (m.remote_host, m.external_port, m.protocol, m.internal_client, m.internal_port)
            if key in seen:
                continue
            seen.add(key)
            mappings.append(m)

            if on_progress:
                on_progress(len(mappings))

        return mappings

    # ---- 便捷方法 ----

    def add_or_replace(self, mapping: PortMapping) -> None:
        """添加映射;若外部端口已被占用则先删掉旧规则再添加。

        返回时保证"该外部端口现在指向 mapping 指定的内网目标"。
        """
        try:
            self.add_port_mapping(
                external_port=mapping.external_port,
                internal_port=mapping.internal_port,
                internal_client=mapping.internal_client,
                protocol=mapping.protocol,
                description=mapping.description,
                remote_host=mapping.remote_host,
                lease_duration=mapping.lease_duration,
            )
            return
        except SOAPError as exc:
            if exc.code not in (718, 724, 725, 501):
                raise IGDError(f"添加端口映射失败:{exc}") from exc

        # 冲突:先删掉同(端口, 协议)的旧规则,再重试
        try:
            self.delete_port_mapping(
                mapping.external_port, mapping.protocol, mapping.remote_host
            )
        except (SOAPError, IGDError):
            pass

        try:
            self.add_port_mapping(
                external_port=mapping.external_port,
                internal_port=mapping.internal_port,
                internal_client=mapping.internal_client,
                protocol=mapping.protocol,
                description=mapping.description,
                remote_host=mapping.remote_host,
                lease_duration=mapping.lease_duration,
            )
        except SOAPError as exc:
            raise IGDError(f"添加端口映射失败:{exc}") from exc


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
