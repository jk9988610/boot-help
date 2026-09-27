#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OM6239 BootCreator 强制进 Boot 引脚助手

从原理图填入如 20,21,26（即 P20/P21/P26），自动计算：
  PAD_PU_CTRL / PAD_PU_CTRL_1 / OUTENCLR / KEY_MASK / OUTENSET / DATAOUT
并可写回 binToBin.exe 窗口对应编辑框。
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional, Sequence, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "boot_force_pin_helper.json"
# BootCreator 本体仍在原目录；助手已独立放到 E:\tools\boot-helper
DEFAULT_EXE = Path(r"E:\OM6239_BootCreator_V05\binToBin.exe")
WINDOW_TITLE = "OM6239BootLoader生成工具v0.5"

# Qt automation_id（已实测）
AID = {
    "pu0": "MainWindow.centralwidget.groupBox_3.LE_PU_0",
    "pu1": "MainWindow.centralwidget.groupBox_3.LE_PU_1",
    "out_en": "MainWindow.centralwidget.groupBox_3.LE_OUT_EN",
    "out_clr": "MainWindow.centralwidget.groupBox_3.LE_OUT_CLR",
    "key_mask": "MainWindow.centralwidget.groupBox_3.LE_KEY_MASK",
    "reserve": "MainWindow.centralwidget.groupBox_3.LE_RESERVE",
    "data_out": "MainWindow.centralwidget.groupBox_3.LE_DATA_OUT",
    "project_id": "MainWindow.centralwidget.groupBox_3.LE_PROJECT_ID",
    "project_name": "MainWindow.centralwidget.groupBox_3.LE_PROJECT_NAME",
    "show": "MainWindow.centralwidget.groupBox_3.PB_showUI",
    "boot_v30": "MainWindow.centralwidget.groupBox_3.BT_out_boot_v30",
    "boot_v20": "MainWindow.centralwidget.groupBox_3.BT_out_boot",
}

_WM_SETTEXT = 0x000C
_BM_CLICK = 0x00F5
_user32 = ctypes.windll.user32


def enable_high_dpi() -> None:
    """进程启动时就切到高 DPI，避免后续 pywinauto 交互时窗口突然缩放。"""
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    try:
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass
class BootRegs:
    pins_port: List[str]          # ["P20", "P21", "P26"]
    linear: List[int]             # [16, 17, 22]
    pu0: int
    pu1: int
    out_en: int
    out_clr: int
    key_mask: int
    reserve: int
    data_out: int

    def as_hex_fields(self) -> dict:
        return {
            "PU0": f"{self.pu0:08X}",
            "PU1": f"{self.pu1:08X}",
            "OUT_EN": f"{self.out_en:08X}",
            "OUT_CLR": f"{self.out_clr:08X}",
            "KEY_MASK": f"{self.key_mask:08X}",
            "RESERVE": f"{self.reserve:08X}",
            "DATA_OUT": f"{self.data_out:08X}",
        }


def load_config() -> dict:
    cfg = {
        "exe_path": str(DEFAULT_EXE if DEFAULT_EXE.exists() else r"E:\OM6239_BootCreator_V05\binToBin.exe"),
        "last_pins": "20,21,26",
        "project_id": "0457",
        "project_name": "CH04",
        "out_en": "00000000",
        "data_out": "FFFFFFFF",
        "reserve": "00000000",
        "auto_show": True,
        "auto_boot_v30": False,
    }
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_port_pins(text: str) -> List[Tuple[str, int]]:
    """
    解析原理图端口号：20 / P20 / p20 → 线性位号 16
    规则：十位=端口(0..3)，个位=脚号(0..7)，线性位 = port*8 + bit
    """
    tokens = re.findall(r"[Pp]?\s*([0-3])\s*([0-7])", text)
    if not tokens:
        raise ValueError("请输入如 20,21,26 或 P20 P21 P26（端口十位 0-3，个位 0-7）")

    seen: Set[int] = set()
    out: List[Tuple[str, int]] = []
    for port_s, bit_s in tokens:
        port, bit = int(port_s), int(bit_s)
        linear = port * 8 + bit
        if linear in seen:
            continue
        seen.add(linear)
        out.append((f"P{port}{bit}", linear))
    if not out:
        raise ValueError("未解析到有效引脚")
    return out


def calc_regs(
    pins_text: str,
    out_en: int = 0,
    data_out: int = 0xFFFFFFFF,
    reserve: int = 0,
    pull_code: int = 0b01,
) -> BootRegs:
    """
    pull_code: 每脚 2bit，手册常用 01 = 150K 上拉。
    KEY_MASK / OUTENCLR：每脚 1bit，置 1 表示该脚参与强制进 Boot（输入）。
    """
    parsed = parse_port_pins(pins_text)
    pu0 = pu1 = 0
    key_mask = 0
    names: List[str] = []
    linears: List[int] = []

    for name, linear in parsed:
        names.append(name)
        linears.append(linear)
        key_mask |= 1 << linear
        if linear < 16:
            # PAD_PU_CTRL：P00-P07, P10-P17 → 线性 0..15，每脚 2bit
            pu0 |= (pull_code & 0b11) << (linear * 2)
        else:
            # PAD_PU_CTRL_1：P20-P27, P30-P37 → 线性 16..31，映射到寄存器内 0..15
            pu1 |= (pull_code & 0b11) << ((linear - 16) * 2)

    return BootRegs(
        pins_port=names,
        linear=linears,
        pu0=pu0,
        pu1=pu1,
        out_en=out_en & 0xFFFFFFFF,
        out_clr=key_mask,      # 与 KEY_MASK 相同：强制 Boot 脚配成输入
        key_mask=key_mask,
        reserve=reserve & 0xFFFFFFFF,
        data_out=data_out & 0xFFFFFFFF,
    )


def format_show_grid(active: Sequence[int], title: str) -> str:
    """复刻 BootCreator SHOW 日志的 4 行 × 8 列布局。"""
    lines = [title, ""]
    for port in range(4):
        cells = []
        for bit in range(8):
            linear = port * 8 + bit
            cells.append(f"P{port}{bit}" if linear in active else "---")
        lines.append(" ".join(cells))
    lines.append("-" * 31)
    return "\n".join(lines)


def _list_boot_hwnds() -> List[int]:
    from pywinauto import Desktop

    hwnds: List[int] = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text() or ""
        except Exception:
            continue
        if WINDOW_TITLE in title:
            try:
                hwnds.append(int(w.handle))
            except Exception:
                pass
    return hwnds


def wrap_boot_window(hwnd: int):
    """Desktop().windows() 得到的 UIAWrapper 没有 child_window，需用 Application.connect。"""
    from pywinauto import Application

    app = Application(backend="uia").connect(handle=hwnd)
    return app.window(handle=hwnd)


def find_boot_window():
    hwnds = _list_boot_hwnds()
    if not hwnds:
        return None
    return wrap_boot_window(hwnds[0])


def _map_auto_ids(win) -> dict:
    """一次遍历窗口控件，建立 auto_id -> (control_type, hwnd/wrapper) 映射。"""
    mapping = {}
    root = win.wrapper_object()
    for el in root.descendants():
        try:
            info = el.element_info
            aid = getattr(info, "automation_id", None) or ""
            if not aid:
                continue
            mapping[aid] = el
        except Exception:
            continue
    return mapping


def _set_text_fast(el, value: str) -> None:
    hwnd = int(getattr(el.element_info, "handle", 0) or 0)
    if hwnd:
        _user32.SendMessageW(hwnd, _WM_SETTEXT, 0, value)
        return
    try:
        el.iface_value.SetValue(value)
    except Exception:
        el.set_edit_text(value)


def _invoke_fast(el) -> None:
    try:
        el.invoke()
        return
    except Exception:
        pass
    try:
        el.click()
    except Exception:
        el.click_input()


def _click_button(el) -> None:
    """顺序点击：优先 Invoke/BM_CLICK，避免 click_input 触发 DPI 缩放跳动。"""
    try:
        el.invoke()
        return
    except Exception:
        pass
    hwnd = int(getattr(el.element_info, "handle", 0) or 0)
    if hwnd:
        _user32.PostMessageW(hwnd, _BM_CLICK, 0, 0)
        return
    try:
        el.click()
    except Exception:
        el.click_input()


def ensure_boot_creator(exe_path: str):
    win = find_boot_window()
    if win is not None:
        return win

    if not Path(exe_path).is_file():
        raise FileNotFoundError(f"找不到 BootCreator：{exe_path}")

    subprocess.Popen([exe_path], cwd=str(Path(exe_path).parent))
    for _ in range(40):
        time.sleep(0.25)
        win = find_boot_window()
        if win is not None:
            return win
    raise TimeoutError("已启动 binToBin.exe，但未找到窗口，请手动点一下该窗口后再试")


def open_bin_folder(exe_path: str) -> Path:
    """打开 binToBin.exe 所在目录（资源管理器）。"""
    folder = Path(exe_path).resolve().parent
    if not folder.is_dir():
        raise FileNotFoundError(f"目录不存在：{folder}")
    # explorer 打开后会自己置前，作为最后停留窗口
    subprocess.Popen(["explorer", str(folder)])
    return folder


def fill_boot_creator(
    exe_path: str,
    regs: BootRegs,
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
    click_show: bool = True,
    click_boot_v30: bool = False,
    click_boot_v20: bool = False,
    open_folder_after_output: bool = False,
) -> dict:
    hwnds = _list_boot_hwnds()
    if not hwnds:
        ensure_boot_creator(exe_path)
        hwnds = _list_boot_hwnds()
    if not hwnds:
        raise TimeoutError("未找到 BootCreator 窗口")

    win = wrap_boot_window(hwnds[0])
    controls = _map_auto_ids(win)

    fields = regs.as_hex_fields()
    pairs = [
        (AID["pu0"], fields["PU0"]),
        (AID["pu1"], fields["PU1"]),
        (AID["out_en"], fields["OUT_EN"]),
        (AID["out_clr"], fields["OUT_CLR"]),
        (AID["key_mask"], fields["KEY_MASK"]),
        (AID["reserve"], fields["RESERVE"]),
        (AID["data_out"], fields["DATA_OUT"]),
    ]
    if project_id:
        pairs.append((AID["project_id"], project_id.strip().upper()))
    if project_name:
        pairs.append((AID["project_name"], project_name.strip()))

    missing = [aid for aid, _ in pairs if aid not in controls]
    if missing:
        raise RuntimeError("BootCreator 控件未就绪：" + ", ".join(m.rsplit(".", 1)[-1] for m in missing))

    for aid, value in pairs:
        _set_text_fast(controls[aid], value)

    # 同时勾选时必须先 SHOW，等日志刷完再点输出，不能连点
    do_output = bool(click_boot_v30 or click_boot_v20)
    do_show = bool(click_show or do_output)
    if do_show:
        if AID["show"] not in controls:
            raise RuntimeError("找不到 SHOW 按钮")
        time.sleep(0.2)
        _click_button(controls[AID["show"]])
        time.sleep(1.0)

    if click_boot_v30:
        if AID["boot_v30"] not in controls:
            raise RuntimeError("找不到 输出BOOT（V30） 按钮")
        _click_button(controls[AID["boot_v30"]])
    elif click_boot_v20:
        if AID["boot_v20"] not in controls:
            raise RuntimeError("找不到 输出BOOT 按钮")
        _click_button(controls[AID["boot_v20"]])

    opened_folder = None
    if open_folder_after_output and do_output:
        # 等 bin 写完再开目录，最后停留在资源管理器
        time.sleep(0.8)
        opened_folder = open_bin_folder(exe_path)
    elif do_output or do_show:
        # 不抢焦点回助手；尽量留在 BootCreator
        try:
            win.set_focus()
        except Exception:
            pass

    return {"hwnd": hwnds[0], "controls": controls, "opened_folder": opened_folder}


PORT_BYTE_LABEL = {
    0: "P00-P07",
    1: "P10-P17",
    2: "P20-P27",
    3: "P30-P37",
}


def format_user_pin_bytes(regs: BootRegs) -> List[str]:
    """按用户实际填入的端口组，打印对应 KEY_MASK 字节。"""
    ports: Set[int] = set()
    pins_by_port: dict = {0: [], 1: [], 2: [], 3: []}
    for name, linear in zip(regs.pins_port, regs.linear):
        port = linear // 8
        ports.add(port)
        pins_by_port[port].append(name)

    lines: List[str] = []
    for port in sorted(ports):
        byte_val = (regs.key_mask >> (port * 8)) & 0xFF
        label = PORT_BYTE_LABEL[port]
        pins = ", ".join(pins_by_port[port])
        lines.append(f"  其中 {label} 字节 = {byte_val:02X}  （{pins}）")

    if any(x < 16 for x in regs.linear):
        lines.append(f"  PU0 = {regs.pu0:08X}")
    if any(x >= 16 for x in regs.linear):
        lines.append(f"  PU1 = {regs.pu1:08X}")
    return lines


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("OM6239 强制 Boot 引脚助手")
        self.geometry("900x800")
        self.minsize(860, 760)
        self.cfg = load_config()
        self.regs: Optional[BootRegs] = None

        self._build()
        self._recalc()

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        row0 = ttk.LabelFrame(frm, text="BootCreator")
        row0.pack(fill=tk.X, **pad)
        self.exe_var = tk.StringVar(value=self.cfg.get("exe_path", ""))
        ttk.Label(row0, text="binToBin.exe").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(row0, textvariable=self.exe_var).grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        ttk.Button(row0, text="浏览…", command=self._browse_exe).grid(row=0, column=2, padx=6, pady=6)
        ttk.Button(row0, text="打开软件", command=self._open_exe).grid(row=0, column=3, padx=6, pady=6)
        row0.columnconfigure(1, weight=1)

        row1 = ttk.LabelFrame(frm, text="从原理图填写强制 Boot 引脚（例：20,21,26）")
        row1.pack(fill=tk.X, **pad)
        self.pins_var = tk.StringVar(value=self.cfg.get("last_pins", "20,21,26"))
        ttk.Entry(row1, textvariable=self.pins_var, font=("Consolas", 14)).grid(
            row=0, column=0, sticky="ew", padx=6, pady=6
        )
        ttk.Button(row1, text="计算", command=self._recalc).grid(row=0, column=1, padx=6, pady=6)
        row1.columnconfigure(0, weight=1)

        row1b = ttk.Frame(row1)
        row1b.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=4)
        self.pid_var = tk.StringVar(value=self.cfg.get("project_id", "0457"))
        self.pname_var = tk.StringVar(value=self.cfg.get("project_name", "CH04"))
        self.outen_var = tk.StringVar(value=self.cfg.get("out_en", "00000000"))
        self.dataout_var = tk.StringVar(value=self.cfg.get("data_out", "FFFFFFFF"))
        self.reserve_var = tk.StringVar(value=self.cfg.get("reserve", "00000000"))
        self.autoshow_var = tk.BooleanVar(value=bool(self.cfg.get("auto_show", True)))
        self.autoboot_v30_var = tk.BooleanVar(value=bool(self.cfg.get("auto_boot_v30", False)))
        self.autofolder_var = tk.BooleanVar(value=bool(self.cfg.get("auto_open_folder", False)))

        for i, (lab, var, w) in enumerate(
            [
                ("项目编号", self.pid_var, 8),
                ("项目名称", self.pname_var, 10),
                ("OUTENSET", self.outen_var, 10),
                ("DATAOUT", self.dataout_var, 10),
                ("Reserve", self.reserve_var, 10),
            ]
        ):
            ttk.Label(row1b, text=lab).grid(row=0, column=i * 2, sticky="e", padx=(8, 2))
            ttk.Entry(row1b, textvariable=var, width=w).grid(row=0, column=i * 2 + 1, sticky="w")

        opts = ttk.Frame(row1b)
        opts.grid(row=1, column=0, columnspan=10, sticky="w", pady=4)
        ttk.Checkbutton(opts, text="填入后自动 SHOW", variable=self.autoshow_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(opts, text="输出BOOT（V30）", variable=self.autoboot_v30_var).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(opts, text="输出后打开目录", variable=self.autofolder_var).pack(side=tk.LEFT)

        mid = ttk.Frame(frm)
        mid.pack(fill=tk.BOTH, expand=True, **pad)

        left = ttk.LabelFrame(mid, text="计算结果")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
        self.result = tk.Text(left, width=42, height=22, font=("Consolas", 10), wrap=tk.NONE)
        self.result.pack(fill=tk.Y, expand=True, padx=6, pady=6)

        right = ttk.LabelFrame(mid, text="引脚预览（同 SHOW 布局）")
        right.pack(side=tk.LEFT, fill=tk.Y)
        self.grid_view = tk.Text(right, width=34, height=22, font=("Consolas", 10), wrap=tk.NONE)
        self.grid_view.pack(fill=tk.Y, expand=True, padx=6, pady=6)

        acts = ttk.Frame(frm)
        acts.pack(fill=tk.X, **pad)
        ttk.Button(acts, text="填入 BootCreator 窗口", command=self._fill).pack(side=tk.LEFT, padx=4)
        ttk.Button(acts, text="复制计算结果", command=self._copy).pack(side=tk.LEFT, padx=4)
        ttk.Button(acts, text="保存默认值", command=self._persist).pack(side=tk.LEFT, padx=4)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status).pack(anchor="w", padx=4)

        self.pins_var.trace_add("write", lambda *_: self._recalc(silent=True))

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 binToBin.exe",
            filetypes=[("可执行文件", "*.exe"), ("全部", "*.*")],
            initialdir=str(Path(self.exe_var.get()).parent if self.exe_var.get() else SCRIPT_DIR),
        )
        if path:
            self.exe_var.set(path)
            self._persist()

    def _open_exe(self) -> None:
        try:
            ensure_boot_creator(self.exe_var.get())
            self.status.set("已打开 BootCreator")
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _parse_hex32(self, s: str, name: str) -> int:
        s = (s or "").strip().replace("0x", "").replace("0X", "")
        if not s:
            return 0
        try:
            v = int(s, 16)
        except ValueError as e:
            raise ValueError(f"{name} 不是合法十六进制：{s}") from e
        return v & 0xFFFFFFFF

    def _recalc(self, silent: bool = False) -> None:
        try:
            regs = calc_regs(
                self.pins_var.get(),
                out_en=self._parse_hex32(self.outen_var.get(), "OUTENSET"),
                data_out=self._parse_hex32(self.dataout_var.get(), "DATAOUT"),
                reserve=self._parse_hex32(self.reserve_var.get(), "Reserve"),
            )
        except Exception as e:
            if not silent:
                self.status.set(str(e))
            return

        self.regs = regs
        fields = regs.as_hex_fields()
        lines = [
            f"引脚: {', '.join(regs.pins_port)}",
            f"线性位: {', '.join(str(x) for x in regs.linear)}",
            "",
            f"PAD_PU_CTRL   = {fields['PU0']}",
            f"PAD_PU_CTRL_1 = {fields['PU1']}",
            f"OUTENSET      = {fields['OUT_EN']}",
            f"OUTENCLR      = {fields['OUT_CLR']}",
            f"KEY_MASK      = {fields['KEY_MASK']}",
            f"Reserve       = {fields['RESERVE']}",
            f"DATAOUT       = {fields['DATA_OUT']}",
            "",
            "分字节核对（高→低）：",
            f"  KEY_MASK bytes = {' '.join(f'{(regs.key_mask >> (8*i)) & 0xFF:02X}' for i in (3, 2, 1, 0))}",
        ]
        lines.extend(format_user_pin_bytes(regs))

        self.result.configure(state=tk.NORMAL)
        self.result.delete("1.0", tk.END)
        self.result.insert("1.0", "\n".join(lines))

        active = set(regs.linear)
        preview = "\n\n".join(
            [
                format_show_grid(active, "-----------上拉IO口------------"),
                format_show_grid([], "-----------输出IO口------------"),
                format_show_grid(active, "------强制进入BOOT->IO口-------"),
            ]
        )
        self.grid_view.configure(state=tk.NORMAL)
        self.grid_view.delete("1.0", tk.END)
        self.grid_view.insert("1.0", preview)
        self.status.set("计算完成")

    def _persist(self) -> None:
        self.cfg.update(
            {
                "exe_path": self.exe_var.get().strip(),
                "last_pins": self.pins_var.get().strip(),
                "project_id": self.pid_var.get().strip(),
                "project_name": self.pname_var.get().strip(),
                "out_en": self.outen_var.get().strip().upper(),
                "data_out": self.dataout_var.get().strip().upper(),
                "reserve": self.reserve_var.get().strip().upper(),
                "auto_show": bool(self.autoshow_var.get()),
                "auto_boot_v30": bool(self.autoboot_v30_var.get()),
                "auto_open_folder": bool(self.autofolder_var.get()),
            }
        )
        save_config(self.cfg)
        self.status.set(f"已保存到 {CONFIG_PATH.name}")

    def _fill(self) -> None:
        self._recalc()
        if self.regs is None:
            messagebox.showerror("无法填入", "请先输入正确引脚")
            return
        self._persist()
        # 记住填入前尺寸，避免操作 BootCreator 后本窗口被 DPI 缩放改小
        geo_before = self.geometry()
        try:
            t0 = time.perf_counter()
            do_v30 = bool(self.autoboot_v30_var.get())
            do_folder = bool(self.autofolder_var.get())
            info = fill_boot_creator(
                self.exe_var.get().strip(),
                self.regs,
                project_id=self.pid_var.get(),
                project_name=self.pname_var.get(),
                click_show=bool(self.autoshow_var.get()),
                click_boot_v30=do_v30,
                open_folder_after_output=do_folder,
            )
            ms = (time.perf_counter() - t0) * 1000
            msg = f"已后台填入（{ms:.0f} ms）"
            if self.autoshow_var.get() or do_v30:
                msg += " + SHOW"
            if do_v30:
                msg += " → 输出BOOT（V30）"
            if info.get("opened_folder"):
                msg += f" → 已打开目录 {info['opened_folder']}"
            self.status.set(msg)
        except Exception as e:
            messagebox.showerror("填入失败", str(e))
        finally:
            # 只恢复尺寸，不切回助手窗口（留在 BootCreator / 目录）
            try:
                self.geometry(geo_before)
                self.update_idletasks()
            except Exception:
                pass

    def _copy(self) -> None:
        text = self.result.get("1.0", tk.END).strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set("已复制计算结果")


def main() -> int:
    # 允许无界面自检：python boot_force_pin_helper.py --calc 20,21,26
    if len(sys.argv) >= 3 and sys.argv[1] == "--calc":
        regs = calc_regs(sys.argv[2])
        for k, v in regs.as_hex_fields().items():
            print(f"{k}={v}")
        print("---")
        for line in format_user_pin_bytes(regs):
            print(line)
        return 0

    enable_high_dpi()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
