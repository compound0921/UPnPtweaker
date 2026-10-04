"""tkinter 图形界面。

所有网络操作都放在后台线程里跑,结果通过队列回到主线程刷新界面,
避免点一下卡三秒。
"""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlparse

from .gateway import Gateway, find_gateways
from .igd import PortMapping, guess_local_ip
from .soap import SOAPError

CONFIG_PATH = Path.home() / ".upnp_portmapper.json"

LEASE_PRESETS = ("永久 (0)", "1 小时", "1 天", "7 天", "30 天")
LEASE_VALUES = {
    "永久 (0)": 0,
    "1 小时": 3600,
    "1 天": 86400,
    "7 天": 604800,
    "30 天": 2592000,
}

# 常用端口预设:(说明, 端口, 协议)
PORT_PRESETS = {
    "自定义": None,
    "远程桌面 (3389)": ("远程桌面", 3389, "TCP"),
    "HTTP (80)": ("Web 服务", 80, "TCP"),
    "HTTPS (443)": ("Web 服务", 443, "TCP"),
    "Minecraft (25565)": ("Minecraft 服务器", 25565, "TCP"),
    "SSH (22)": ("SSH", 22, "TCP"),
    "FTP (21)": ("FTP", 21, "TCP"),
    "BitTorrent (6881)": ("BT", 6881, "TCP"),
}


def parse_lease(text: str) -> int:
    """把租期输入框的文字转成秒数。"""
    text = (text or "").strip()
    if not text:
        return 0
    if text in LEASE_VALUES:
        return LEASE_VALUES[text]
    if text.isdigit():
        return int(text)
    raise ValueError(f"租期格式无法识别:{text!r}(可填 0 表示永久,或直接填秒数)")


class AsyncRunner:
    """把耗时函数丢到后台线程,回调在主线程执行。"""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.busy = 0
        self.root.after(60, self._pump)

    def run(self, fn, on_done=None, on_error=None) -> None:
        self.busy += 1

        def worker() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - 统一回传到界面
                self.queue.put(("error", on_error, exc))
            else:
                self.queue.put(("done", on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    def post(self, fn) -> None:
        """把一个普通回调排到主线程执行(用于状态输出)。"""
        self.queue.put(("call", fn, None))

    def _pump(self) -> None:
        while True:
            try:
                kind, callback, payload = self.queue.get_nowait()
            except queue.Empty:
                break

            if kind == "done":
                self.busy = max(0, self.busy - 1)
            elif kind == "error":
                self.busy = max(0, self.busy - 1)

            if callback is None:
                continue
            try:
                callback(payload)
            except Exception as exc:  # noqa: BLE001 - 回调出错不该打死循环
                print(f"回调执行出错: {exc!r}")

        self.root.after(60, self._pump)


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.pack(fill="both", expand=True)

        self.runner = AsyncRunner(master)
        self.gateways: list[Gateway] = []
        self.current: Gateway | None = None
        self.mappings: dict[str, PortMapping] = {}   # treeview iid -> 映射
        self.config = self._load_config()

        self._build_ui()
        self._apply_config()

        self.log("准备就绪。点击「搜索设备」开始。")

    # ---------- 界面搭建 ----------

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(4, weight=2)

        # --- 第 1 行:设备选择 ---
        top = ttk.LabelFrame(self, text="路由器", padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        self.btn_scan = ttk.Button(top, text="搜索设备", command=self.on_scan, width=12)
        self.btn_scan.grid(row=0, column=0, padx=(0, 8))

        self.cmb_gateway = ttk.Combobox(top, state="readonly", values=[])
        self.cmb_gateway.grid(row=0, column=1, sticky="ew")
        self.cmb_gateway.bind("<<ComboboxSelected>>", self.on_gateway_selected)

        self.lbl_status = ttk.Label(top, text="未连接", foreground="#888")
        self.lbl_status.grid(row=0, column=2, padx=(8, 0))

        # --- 第 2 行:外网信息 ---
        info = ttk.Frame(self)
        info.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        ttk.Label(info, text="外网 IP:").pack(side="left")
        self.lbl_ext_ip = ttk.Label(info, text="—", font=("Consolas", 10, "bold"))
        self.lbl_ext_ip.pack(side="left", padx=(4, 12))

        ttk.Label(info, text="连接状态:").pack(side="left")
        self.lbl_conn = ttk.Label(info, text="—")
        self.lbl_conn.pack(side="left", padx=(4, 12))

        self.lbl_local_ip = ttk.Label(info, text="本机 IP: —", foreground="#555")
        self.lbl_local_ip.pack(side="left")

        self.btn_refresh = ttk.Button(info, text="刷新", command=self.refresh_all, width=8)
        self.btn_refresh.pack(side="right")
        self.btn_refresh.state(["disabled"])

        # --- 第 3 行:映射列表 ---
        list_frame = ttk.LabelFrame(self, text="现有端口映射", padding=6)
        list_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        columns = ("desc", "proto", "ext", "client", "int", "lease", "enabled")
        headings = {
            "desc": ("描述", 170),
            "proto": ("协议", 55),
            "ext": ("外部端口", 75),
            "client": ("内部 IP", 120),
            "int": ("内部端口", 75),
            "lease": ("租期", 80),
            "enabled": ("状态", 60),
        }
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            text, width = headings[col]
            self.tree.heading(col, text=text)
            anchor = "w" if col in ("desc", "client") else "center"
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "desc"))
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.bind("<Double-1>", self.on_row_double_click)

        # --- 第 4 行:操作区 ---
        ops = ttk.LabelFrame(self, text="新增 / 修改映射", padding=8)
        ops.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        for c in (1, 3, 5):
            ops.columnconfigure(c, weight=1)

        self.var_desc = tk.StringVar()
        self.var_proto = tk.StringVar(value="TCP")
        self.var_ext = tk.StringVar()
        self.var_int = tk.StringVar()
        self.var_client = tk.StringVar()
        self.var_lease = tk.StringVar(value="永久 (0)")
        self.var_remote = tk.StringVar()
        self.var_preset = tk.StringVar(value="自定义")

        ttk.Label(ops, text="常用:").grid(row=0, column=0, sticky="w")
        cmb_preset = ttk.Combobox(
            ops, textvariable=self.var_preset, state="readonly",
            values=list(PORT_PRESETS), width=20,
        )
        cmb_preset.grid(row=0, column=1, sticky="w", padx=(4, 12))
        cmb_preset.bind("<<ComboboxSelected>>", self.on_preset)

        ttk.Label(ops, text="协议:").grid(row=0, column=2, sticky="e")
        ttk.Combobox(
            ops, textvariable=self.var_proto, state="readonly",
            values=("TCP", "UDP"), width=8,
        ).grid(row=0, column=3, sticky="w", padx=(4, 12))

        ttk.Label(ops, text="租期:").grid(row=0, column=4, sticky="e")
        ttk.Combobox(
            ops, textvariable=self.var_lease, values=LEASE_PRESETS, width=12,
        ).grid(row=0, column=5, sticky="w", padx=(4, 0))

        ttk.Label(ops, text="描述:").grid(row=1, column=0, sticky="e", pady=(6, 0))
        ttk.Entry(ops, textvariable=self.var_desc).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(4, 12), pady=(6, 0)
        )

        ttk.Label(ops, text="外部端口:").grid(row=1, column=3, sticky="e", pady=(6, 0))
        ttk.Entry(ops, textvariable=self.var_ext, width=10).grid(
            row=1, column=4, columnspan=2, sticky="w", padx=(4, 0), pady=(6, 0)
        )

        ttk.Label(ops, text="内部 IP:").grid(row=2, column=0, sticky="e", pady=(6, 0))
        ttk.Entry(ops, textvariable=self.var_client).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(4, 12), pady=(6, 0)
        )

        ttk.Label(ops, text="内部端口:").grid(row=2, column=3, sticky="e", pady=(6, 0))
        ttk.Entry(ops, textvariable=self.var_int, width=10).grid(
            row=2, column=4, columnspan=2, sticky="w", padx=(4, 0), pady=(6, 0)
        )

        ttk.Label(ops, text="远程主机:").grid(row=3, column=0, sticky="e", pady=(6, 0))
        ttk.Entry(ops, textvariable=self.var_remote).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(4, 12), pady=(6, 0)
        )
        ttk.Label(ops, text="(留空 = 允许任何来源)", foreground="#888").grid(
            row=3, column=3, columnspan=3, sticky="w", padx=(4, 0), pady=(6, 0)
        )

        buttons = ttk.Frame(ops)
        buttons.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(10, 0))

        self.btn_add = ttk.Button(buttons, text="添加 / 覆盖映射", command=self.on_add, width=16)
        self.btn_add.pack(side="left")

        self.btn_delete = ttk.Button(buttons, text="删除选中", command=self.on_delete, width=12)
        self.btn_delete.pack(side="left", padx=(8, 0))

        self.btn_fill = ttk.Button(buttons, text="用选中项填充表单", command=self.on_fill_from_selection, width=18)
        self.btn_fill.pack(side="left", padx=(8, 0))

        ttk.Button(buttons, text="清空表单", command=self.clear_form, width=10).pack(side="left", padx=(8, 0))

        self.btn_check = ttk.Button(buttons, text="检查该端口", command=self.on_check_port, width=12)
        self.btn_check.pack(side="left", padx=(8, 0))

        # --- 第 5 行:日志 ---
        log_frame = ttk.LabelFrame(self, text="日志", padding=6)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.txt_log = tk.Text(log_frame, height=8, wrap="word", state="disabled",
                               background="#1e1e1e", foreground="#d4d4d4",
                               font=("Consolas", 9), relief="flat")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=log_sb.set)

    # ---------- 配置读写 ----------

    def _load_config(self) -> dict:
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_config(self) -> None:
        data = {
            "description": self.var_desc.get(),
            "protocol": self.var_proto.get(),
            "external_port": self.var_ext.get(),
            "internal_port": self.var_int.get(),
            "internal_client": self.var_client.get(),
            "lease": self.var_lease.get(),
            "remote_host": self.var_remote.get(),
            "gateway_index": self.cmb_gateway.current(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.log(f"配置保存失败: {exc}")

    def _apply_config(self) -> None:
        c = self.config
        self.var_desc.set(c.get("description", ""))
        self.var_proto.set(c.get("protocol", "TCP"))
        self.var_ext.set(c.get("external_port", ""))
        self.var_int.set(c.get("internal_port", ""))
        self.var_client.set(c.get("internal_client", ""))
        self.var_lease.set(c.get("lease", "永久 (0)"))
        self.var_remote.set(c.get("remote_host", ""))

    # ---------- 工具 ----------

    def log(self, message: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"{message}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def log_threadsafe(self, message: str) -> None:
        """后台线程里也能安全地写日志。"""
        self.runner.post(lambda _=None, m=message: self.log(m))

    def _set_busy(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        for widget in (self.btn_scan, self.btn_add, self.btn_delete, self.btn_refresh,
                       self.btn_check, self.btn_fill):
            widget.state(state)
        if not busy and self.current is None:
            self.btn_refresh.state(["disabled"])
            self.btn_add.state(["disabled"])
            self.btn_delete.state(["disabled"])
            self.btn_check.state(["disabled"])

    def _require_gateway(self) -> Gateway | None:
        if self.current is None:
            messagebox.showinfo("还没有连接路由器", "请先点击「搜索设备」。")
            return None
        return self.current

    # ---------- 事件 ----------

    def on_scan(self) -> None:
        self._set_busy(True)
        self.log("——— 开始搜索 ———")

        def work():
            return find_gateways(timeout=2.5, on_status=self.log_threadsafe)

        def done(gateways: list[Gateway]) -> None:
            self._set_busy(False)
            self.gateways = gateways
            self.cmb_gateway.configure(values=[g.display for g in gateways])
            if gateways:
                self.cmb_gateway.current(0)
                self.on_gateway_selected()
            else:
                self.cmb_gateway.set("")

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self.log(f"搜索失败:{exc}")
            messagebox.showerror("搜索失败", str(exc))

        self.runner.run(work, on_done=done, on_error=failed)

    def on_gateway_selected(self, _event=None) -> None:
        idx = self.cmb_gateway.current()
        if idx < 0 or idx >= len(self.gateways):
            return
        self.current = self.gateways[idx]
        device = self.current.device
        self.log(f"已选择:{device.friendly_name} / {device.model_name} ({self.current.service.short_type})")

        # 默认内部 IP:本机在路由器网段的地址
        router_host = urlparse(self.current.service.control_url).hostname or ""
        local_ip = guess_local_ip(router_host)
        if local_ip and not self.var_client.get():
            self.var_client.set(local_ip)
        self.lbl_local_ip.configure(text=f"本机 IP: {local_ip or '未知'}")

        self.refresh_all()

    def refresh_all(self) -> None:
        gw = self.current
        if gw is None:
            return
        self._set_busy(True)

        def work():
            ext_ip = ""
            status = ""
            try:
                ext_ip = gw.igd.get_external_ip()
            except Exception as exc:  # noqa: BLE001
                self.log_threadsafe(f"读取外网 IP 失败:{exc}")
            try:
                info = gw.igd.get_status_info()
                status = info.get("status", "")
            except Exception as exc:  # noqa: BLE001
                self.log_threadsafe(f"读取连接状态失败:{exc}")
            mappings = gw.igd.list_port_mappings()
            return ext_ip, status, mappings

        def done(result) -> None:
            ext_ip, status, mappings = result
            self._set_busy(False)
            self.lbl_ext_ip.configure(text=ext_ip or "—")
            self.lbl_conn.configure(text=status or "—",
                                    foreground="#0a0" if status == "Connected" else "#c60")
            self._populate_tree(mappings)
            self.log(f"已刷新:外网 IP {ext_ip or '未知'},共 {len(mappings)} 条映射。")

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self.log(f"刷新失败:{exc}")
            messagebox.showerror("刷新失败", str(exc))

        self.runner.run(work, on_done=done, on_error=failed)

    def _populate_tree(self, mappings: list[PortMapping]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.mappings.clear()
        for i, m in enumerate(mappings):
            iid = f"m{i}"
            self.mappings[iid] = m
            self.tree.insert(
                "", "end", iid=iid,
                values=(
                    m.description or "(无描述)",
                    m.protocol,
                    m.external_port,
                    m.internal_client,
                    m.internal_port,
                    m.lease_text,
                    "启用" if m.enabled else "停用",
                ),
            )

    def on_row_double_click(self, _event=None) -> None:
        self.on_fill_from_selection()

    def on_fill_from_selection(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("未选中", "请先在列表里选一条映射。")
            return
        m = self.mappings.get(sel[0])
        if m is None:
            return
        self.var_desc.set(m.description)
        self.var_proto.set(m.protocol)
        self.var_ext.set(str(m.external_port))
        self.var_int.set(str(m.internal_port))
        self.var_client.set(m.internal_client)
        self.var_remote.set(m.remote_host)
        self.var_lease.set("永久 (0)" if m.lease_duration <= 0 else str(m.lease_duration))
        self.log(f"已填入表单:外部 {m.external_port}/{m.protocol} → {m.internal_client}:{m.internal_port}")

    def clear_form(self) -> None:
        self.var_desc.set("")
        self.var_ext.set("")
        self.var_int.set("")
        self.var_remote.set("")
        self.var_proto.set("TCP")
        self.var_lease.set("永久 (0)")
        self.var_preset.set("自定义")

    def on_preset(self, _event=None) -> None:
        name = self.var_preset.get()
        preset = PORT_PRESETS.get(name)
        if not preset:
            return
        desc, port, proto = preset
        self.var_desc.set(desc)
        self.var_proto.set(proto)
        self.var_ext.set(str(port))
        self.var_int.set(str(port))

    def on_add(self) -> None:
        gw = self._require_gateway()
        if gw is None:
            return

        try:
            ext = int(self.var_ext.get().strip())
            int_port = int(self.var_int.get().strip()) if self.var_int.get().strip() else ext
            lease = parse_lease(self.var_lease.get())
        except ValueError as exc:
            messagebox.showerror("参数有误", str(exc))
            return

        client = self.var_client.get().strip()
        if not client:
            messagebox.showerror("参数有误", "请填写内部 IP(要转发到的局域网设备地址)。")
            return
        if not 1 <= ext <= 65535 or not 1 <= int_port <= 65535:
            messagebox.showerror("参数有误", "端口必须在 1 ~ 65535 之间。")
            return

        mapping = PortMapping(
            remote_host=self.var_remote.get().strip(),
            external_port=ext,
            protocol=self.var_proto.get().upper(),
            internal_port=int_port,
            internal_client=client,
            enabled=True,
            description=self.var_desc.get().strip() or "upnptweaker",
            lease_duration=lease,
        )

        # 先看看这个外部端口是不是已经有规则,有的话提前告知
        existing = next(
            (m for m in self.mappings.values()
             if m.external_port == ext and m.protocol == mapping.protocol
             and m.remote_host == mapping.remote_host),
            None,
        )
        if existing is not None:
            ok = messagebox.askyesno(
                "端口已被占用",
                f"{ext}/{mapping.protocol} 已有一条规则:\n"
                f"  {existing.description or '(无描述)'}\n"
                f"  → {existing.internal_client}:{existing.internal_port}\n\n"
                f"要用新规则覆盖它吗?",
            )
            if not ok:
                return

        self._set_busy(True)
        self.log(f"正在添加:{ext}/{mapping.protocol} → {client}:{int_port} …")

        def work():
            gw.igd.add_or_replace(mapping)

        def done(_result=None) -> None:
            self._set_busy(False)
            self.log(f"✓ 添加成功:{ext}/{mapping.protocol} → {client}:{int_port}")
            self._save_config()
            self.refresh_all()

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self.log(f"✗ 添加失败:{exc}")
            messagebox.showerror("添加失败", str(exc))

        self.runner.run(work, on_done=done, on_error=failed)

    def on_delete(self) -> None:
        gw = self._require_gateway()
        if gw is None:
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("未选中", "请先在列表里选一条要删除的映射。")
            return
        m = self.mappings.get(sel[0])
        if m is None:
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确定要删除这条映射吗?\n\n"
            f"  {m.description or '(无描述)'}\n"
            f"  {m.external_port}/{m.protocol} → {m.internal_client}:{m.internal_port}",
        ):
            return

        self._set_busy(True)
        self.log(f"正在删除:{m.external_port}/{m.protocol} …")

        def work():
            gw.igd.delete_port_mapping(m.external_port, m.protocol, m.remote_host)

        def done(_result=None) -> None:
            self._set_busy(False)
            self.log(f"✓ 已删除:{m.external_port}/{m.protocol}")
            self.refresh_all()

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            self.log(f"✗ 删除失败:{exc}")
            messagebox.showerror("删除失败", str(exc))

        self.runner.run(work, on_done=done, on_error=failed)

    def on_check_port(self) -> None:
        """检查某个外部端口当前指向哪里。"""
        gw = self._require_gateway()
        if gw is None:
            return
        try:
            ext = int(self.var_ext.get().strip())
        except ValueError:
            messagebox.showerror("参数有误", "请先在「外部端口」里填一个端口号。")
            return
        proto = self.var_proto.get().upper()
        remote = self.var_remote.get().strip()

        self._set_busy(True)

        def work():
            return gw.igd.get_specific_port_mapping(ext, proto, remote)

        def done(m: PortMapping | None) -> None:
            self._set_busy(False)
            if m is None:
                self.log(f"端口 {ext}/{proto} 当前没有映射规则。")
                messagebox.showinfo("查询结果", f"端口 {ext}/{proto} 当前没有映射规则。")
            else:
                text = (
                    f"端口 {ext}/{proto} 当前指向:\n\n"
                    f"  内部地址:{m.internal_client}:{m.internal_port}\n"
                    f"  描述:{m.description or '(无描述)'}\n"
                    f"  租期:{m.lease_text}\n"
                    f"  状态:{'启用' if m.enabled else '停用'}"
                )
                self.log(f"端口 {ext}/{proto} → {m.internal_client}:{m.internal_port}")
                messagebox.showinfo("查询结果", text)

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            if isinstance(exc, SOAPError) and exc.code == 714:
                self.log(f"端口 {ext}/{proto} 当前没有映射规则。")
                return
            self.log(f"✗ 查询失败:{exc}")
            messagebox.showerror("查询失败", str(exc))

        self.runner.run(work, on_done=done, on_error=failed)


def _pick_ui_font(root: tk.Misc) -> str | None:
    """挑一个本机真实存在的中文字体。

    指定一个不存在的字体在 tkinter 里不会报错,只会静默回退成默认字体,
    界面会变得很难看,所以在几个平台各自的常见中文字体里挑一个。
    """
    import tkinter.font as tkfont

    preferred = (
        "Microsoft YaHei UI",   # Windows
        "微软雅黑",
        "PingFang SC",          # macOS
        "Hiragino Sans GB",
        "Noto Sans CJK SC",     # Linux
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
    )
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        return None
    for name in preferred:
        if name in available:
            return name
    return None


def run() -> None:
    root = tk.Tk()
    root.title("UPnPtweaker — UPnP 端口映射工具")
    root.geometry("940x780")
    root.minsize(820, 640)

    try:
        # 让界面在 Windows 上跟随 DPI 缩放,不至于糊/小
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 只在 Windows 上有,其它平台跳过
        pass

    style = ttk.Style()
    for theme in ("vista", "winnative", "aqua", "clam", "default"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break

    font_name = _pick_ui_font(root)
    if font_name:
        style.configure(".", font=(font_name, 10))

    App(root)
    root.mainloop()


if __name__ == "__main__":
    run()
