#!/usr/bin/env python3
"""UPnPtweaker —— UPnP 端口映射工具,启动入口。

直接运行(不带参数)打开图形界面:
    python main.py

也提供命令行模式,方便排查问题:
    python main.py scan                       # 搜索路由器
    python main.py list                       # 列出所有端口映射
    python main.py ip                         # 查看外网 IP
    python main.py add 8080 80 192.168.1.5    # 外部8080 → 192.168.1.5:80
    python main.py del 8080                   # 删除 8080/TCP
"""

from __future__ import annotations

import argparse
import sys

from upnptweaker import IGDError
from upnptweaker.gateway import find_gateways
from upnptweaker.igd import PortMapping


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _status(msg: str) -> None:
    print(f"  · {msg}", flush=True)


def _first_gateway():
    _print("正在搜索路由器…")
    gateways = find_gateways(timeout=2.5, on_status=_status)
    if not gateways:
        raise IGDError("没有找到可用的 WAN 连接服务")
    gw = gateways[0]
    _print(f"使用:{gw.display}")
    _print()
    return gw


def cmd_scan(_args) -> int:
    gateways = find_gateways(timeout=2.5, on_status=_status)
    _print()
    for i, gw in enumerate(gateways):
        dev = gw.device
        _print(f"[{i}] {dev.friendly_name}")
        _print(f"     厂商/型号:{dev.manufacturer} {dev.model_name} {dev.model_number}".rstrip())
        _print(f"     服务:{gw.service.service_type}")
        _print(f"     控制地址:{gw.service.control_url}")
    return 0


def cmd_list(_args) -> int:
    gw = _first_gateway()
    _print(f"外网 IP:{gw.igd.get_external_ip() or '未知'}")
    info = gw.igd.get_status_info()
    _print(f"连接状态:{info.get('status') or '未知'}")
    _print()
    mappings = gw.igd.list_port_mappings()
    if not mappings:
        _print("(路由器上没有任何端口映射)")
        return 0
    _print(f"{'协议':<6}{'外部端口':>8}  {'内部地址':<24}{'租期':<10}描述")
    _print("-" * 88)
    for m in mappings:
        target = f"{m.internal_client}:{m.internal_port}"
        flag = "" if m.enabled else " (停用)"
        _print(f"{m.protocol:<6}{m.external_port:>8}  {target:<24}{m.lease_text:<10}{m.description}{flag}")
    _print()
    _print(f"共 {len(mappings)} 条。")
    return 0


def cmd_ip(_args) -> int:
    gw = _first_gateway()
    _print(f"外网 IP:{gw.igd.get_external_ip() or '未知'}")
    return 0


def cmd_add(args) -> int:
    gw = _first_gateway()
    mapping = PortMapping(
        external_port=args.external_port,
        internal_port=args.internal_port or args.external_port,
        internal_client=args.client,
        protocol=args.protocol.upper(),
        description=args.description or "upnptweaker",
        lease_duration=args.lease,
    )
    _print(f"添加:{mapping.external_port}/{mapping.protocol} → "
           f"{mapping.internal_client}:{mapping.internal_port} (租期 {mapping.lease_text})")
    gw.igd.add_or_replace(mapping)
    _print("✓ 成功")
    return 0


def cmd_del(args) -> int:
    gw = _first_gateway()
    _print(f"删除:{args.external_port}/{args.protocol.upper()}")
    gw.igd.delete_port_mapping(args.external_port, args.protocol.upper())
    _print("✓ 成功")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="UPnPtweaker —— UPnP 端口映射工具(不带参数则打开图形界面)",
        epilog="任意命令后面都可以加 --debug,打印原始 SOAP 报文,"
               "用来确认 UPnP 请求里不含任何认证信息。",
    )
    sub = p.add_subparsers(dest="command")

    sub.add_parser("scan", help="搜索局域网内的 UPnP 路由器")
    sub.add_parser("list", help="列出路由器上所有端口映射")
    sub.add_parser("ip", help="查询路由器外网 IP")

    a = sub.add_parser("add", help="添加端口映射")
    a.add_argument("external_port", type=int, help="外部端口")
    a.add_argument("client", help="内部设备 IP(要转发到的局域网地址)")
    a.add_argument("internal_port", type=int, nargs="?", default=None, help="内部端口(默认同外部端口)")
    a.add_argument("-p", "--protocol", default="TCP", choices=["TCP", "UDP", "tcp", "udp"])
    a.add_argument("-d", "--description", default="", help="规则描述")
    a.add_argument("-l", "--lease", type=int, default=0, help="租期秒数,0 = 永久")

    d = sub.add_parser("del", help="删除端口映射")
    d.add_argument("external_port", type=int, help="外部端口")
    d.add_argument("-p", "--protocol", default="TCP", choices=["TCP", "UDP", "tcp", "udp"])

    return p


def _force_utf8_output() -> None:
    """Windows 控制台默认按 GBK 编码,中文输出会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    argv = list(sys.argv[1:] if argv is None else argv)

    # --debug 是全局开关,先摘出来交给 argparse 之前的这段处理
    if "--debug" in argv:
        argv.remove("--debug")
        from upnptweaker import soap
        soap.set_debug(True)
        _print("(调试模式:打印原始 SOAP 报文 —— 可以看到请求里不含任何认证信息)")

    if not argv:
        # 没有参数 -> 打开图形界面
        from upnptweaker.gui import run
        run()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "scan": cmd_scan,
        "list": cmd_list,
        "ip": cmd_ip,
        "add": cmd_add,
        "del": cmd_del,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 2

    try:
        return handler(args)
    except IGDError as exc:
        print(f"\n错误:{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
