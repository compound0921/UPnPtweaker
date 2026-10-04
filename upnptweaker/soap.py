"""UPnP 的 SOAP 控制调用(纯标准库)。

UPnP 设备的所有操作都是往 controlURL POST 一段 SOAP 报文,
并用 SOAPAction 头指明要调用的服务与动作。
"""

from __future__ import annotations

import http.client
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
UPNP_CONTROL_NS = "urn:schemas-upnp-org:control-1-0"

# 常见的 UPnP 错误码 -> 中文说明
ERROR_HINTS = {
    401: "参数无效(Invalid Action)",
    402: "参数不合法(Invalid Args)",
    403: "动作不存在",
    501: "路由器拒绝执行该动作(Action Failed)",
    606: "动作未获授权",
    701: "收到无效的报文",
    702: "设备不支持该动作",
    703: "服务不可用",
    704: "订阅失败",
    705: "订阅已过期",
    706: "参数(端口/租期等)取值超出允许范围",
    709: "参数值不合法或为空",
    710: "找不到对应的连接实例",
    711: "该连接实例已被占用",
    713: "指定的索引不存在(通常表示端口映射列表已读完)",
    714: "该条目不存在",
    715: "源地址不允许使用通配符",
    716: "路由器不允许外部端口使用通配符",
    718: "端口映射冲突:该外部端口已被占用",
    724: "内外端口必须相同(路由器限制)",
    725: "路由器只支持永久租期,请把租期填 0",
    726: "远程主机只能使用通配符",
    727: "外部端口只能使用通配符",
    728: "路由器不支持静态租期",
    729: "路由器不支持指定远程主机",
    732: "内外端口必须不同",
    733: "该端口映射与已有规则冲突",
}

_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="{env}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body>"
    "</s:Envelope>"
)


class SOAPError(Exception):
    """SOAP 调用失败。若路由器返回了 UPnP 错误码,code 即为该错误码。"""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        http_status: int | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.body = body

    @property
    def is_index_invalid(self) -> bool:
        """索引越界 —— 枚举端口映射时,这就是"读完了"的信号。"""
        return self.code == 713


_DEBUG = False


def set_debug(enabled: bool) -> None:
    """打开后把每次 SOAP 往返的原始报文打到 stdout。

    值得亲眼看一次:请求里**不含任何认证信息** —— 没有密码、没有 token、
    没有 cookie。路由器只凭"这个包来自局域网内部"这一件事就执行了操作。
    这就是 UPnP 一直以来的安全争议所在。
    """
    global _DEBUG
    _DEBUG = enabled


def _trace(direction: str, target: str, payload: str) -> None:
    if not _DEBUG:
        return
    print(f"\n──── {direction} {target} ────", flush=True)
    print(payload, flush=True)


def _local(tag: str) -> str:
    """去掉命名空间前缀,只留本地名。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_envelope(service_type: str, action: str, args: dict) -> str:
    """构造 SOAP 请求正文。"""
    parts = [f'<u:{action} xmlns:u="{_escape(service_type)}">']
    for key, value in args.items():
        parts.append(f"<{key}>{_escape(value)}</{key}>")
    parts.append(f"</u:{action}>")
    return _ENVELOPE.format(env=SOAP_ENVELOPE_NS, body="".join(parts))


def parse_fault(root: ET.Element) -> tuple[int | None, str]:
    """从 SOAP Fault 中提取 (错误码, 描述)。"""
    code = None
    desc = ""
    for elem in root.iter():
        name = _local(elem.tag)
        if name == "errorCode" and elem.text:
            try:
                code = int(elem.text.strip())
            except ValueError:
                pass
        elif name == "errorDescription" and elem.text:
            desc = elem.text.strip()
        elif name == "faultstring" and elem.text and not desc:
            desc = elem.text.strip()
    return code, desc


def call(
    control_url: str,
    service_type: str,
    action: str,
    args: dict | None = None,
    timeout: float = 10.0,
) -> dict[str, str]:
    """调用一个 UPnP 动作,返回 {输出参数名: 值}。

    失败时抛出 SOAPError(带 code 便于上层区分处理)。
    """
    args = args or {}
    body = build_envelope(service_type, action, args).encode("utf-8")

    url = urlparse(control_url)
    if not url.hostname:
        raise SOAPError(f"控制地址不合法: {control_url!r}")

    path = url.path or "/"
    if url.query:
        path = f"{path}?{url.query}"

    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#{action}"',
        "Content-Length": str(len(body)),
        "Connection": "close",
        "User-Agent": "upnptweaker/1.0 UPnP/1.0",
    }

    header_dump = "\n".join(f"{k}: {v}" for k, v in headers.items())
    _trace(
        "SOAP 请求 →",
        f"http://{url.hostname}:{url.port or 80}{path}",
        f"POST {path} HTTP/1.1\n{header_dump}\n\n{body.decode('utf-8')}",
    )

    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    except OSError as exc:
        raise SOAPError(f"无法连接路由器控制地址 {url.hostname}: {exc}") from exc
    finally:
        conn.close()

    text = raw.decode("utf-8", "replace")
    _trace("SOAP 响应 ←", f"HTTP {status}", text)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SOAPError(
            f"路由器返回的报文无法解析(HTTP {status}): {exc}",
            http_status=status,
            body=text[:2000],
        ) from exc

    # 先看是不是 Fault
    fault_root = None
    for elem in root.iter():
        if _local(elem.tag) == "Fault":
            fault_root = elem
            break

    if fault_root is not None:
        code, desc = parse_fault(root)
        hint = ERROR_HINTS.get(code or -1, "")
        msg = desc or hint or "路由器返回了错误"
        if hint and desc and hint != desc:
            msg = f"{desc} —— {hint}"
        raise SOAPError(msg, code=code, http_status=status, body=text[:2000])

    if status != 200:
        raise SOAPError(
            f"路由器返回 HTTP {status}",
            http_status=status,
            body=text[:2000],
        )

    # 取 ActionResponse 里的输出参数
    result: dict[str, str] = {}
    for elem in root.iter():
        # 响应元素的子节点就是输出参数(它的父节点名字以 Response 结尾)
        if _local(elem.tag).endswith("Response"):
            for child in elem:
                result[_local(child.tag)] = (child.text or "").strip()
            break

    return result
