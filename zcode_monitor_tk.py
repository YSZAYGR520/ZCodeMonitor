# -*- coding: utf-8 -*-
"""
zcode_monitor_tk.py — ZCode 监控悬浮窗(tkinter 版, 零外部依赖)

架构: 本进程内 tkinter Canvas 自绘深色卡片 UI; 数据复用 zcode_usage(本地 SQLite +
官方额度接口)。无 pywebview/WebView2/跨语言桥 —— 从根源消除桥竞态; 打包 EXE 仅
依赖 Python 标准库 + tcl/tk, 目标机无需任何运行时。
"""
import datetime
import os
import queue
import sys
import threading

# DPI 感知必须先于 tkinter import, 否则进程 DPI 上下文已被 tk 占用而静默失败
try:
    import ctypes as _ctypes
    _ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        _ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
import tkinter.font as tkfont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zcode_usage as zu

WIN_KEYS = [("today", "今日"), ("7d", "7天"), ("30d", "30天"), ("all", "全部")]


# ---------------------------------------------------------------------------
# 数据组装(后台线程跑, 卡了不拖 UI)
# ---------------------------------------------------------------------------
def _since_ms(key):
    if key == "all":
        return None
    now = zu.now_ms()
    if key == "today":
        mid = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(mid.timestamp() * 1000)
    days = {"7d": 7, "30d": 30}[key]
    return now - days * 24 * 3600 * 1000


def collect_data(win_key):
    """一次全量数据(与旧 Web 版口径一致), 供后台线程调用。
    官方额度/趋势两个网络请求并行执行, 首屏等待 = 单请求耗时。"""
    since = _since_ms(win_key)
    cfg = zu.load_monitor_config()
    panel = zu.compute_model_panels(since_ms=since)
    kpi = zu.overview_kpis(since_ms=since)
    box = {}

    def _quota():
        try:
            box["quota"] = zu.quota_snapshot(cfg=cfg)
        except Exception as ex:
            box["quota_err"] = ex

    def _usage():
        try:
            box["ou"] = zu.fetch_official_usage("24h")
        except Exception as ex:
            box["ou_err"] = ex

    t1 = threading.Thread(target=_quota, daemon=True)
    t2 = threading.Thread(target=_usage, daemon=True)
    t1.start(); t2.start(); t1.join(); t2.join()
    quota = box.get("quota") or {}
    ou = box.get("ou") or {}
    quota["is_peak"] = zu.is_peak_now()
    off = quota.get("official") or {}
    off_ok = bool(off and off.get("ok"))
    if off_ok:
        blk = dict(off["block"])
        wk = dict(off["week"])
        blk.setdefault("burn_tok_h", (quota.get("block") or {}).get("burn_tok_h", 0))
        blk.setdefault("projected_pct", (quota.get("block") or {}).get("projected_pct"))
        blk.setdefault("requests", (quota.get("block") or {}).get("requests", 0))
        wk.setdefault("pct", None)
    else:
        b = quota.get("block") or {}
        qb = b.get("quota") or 0
        used = b.get("est_credits", 0)
        blk = {"used": used, "quota": qb, "remaining": max(qb - used, 0),
               "pct": b.get("pct"), "reset_ms": None, "remaining_ms": b.get("remaining_ms"),
               "burn_tok_h": b.get("burn_tok_h", 0), "projected_pct": b.get("projected_pct"),
               "requests": b.get("requests", 0)}
        w = quota.get("week") or {}
        qw = w.get("quota") or 0
        usedw = w.get("est_credits", 0)
        wk = {"used": usedw, "quota": qw, "remaining": max(qw - usedw, 0),
              "pct": w.get("pct"), "reset_ms": None, "remaining_ms": None}
    quota["blk"], quota["wk"] = blk, wk
    mcp = off.get("mcp") if off_ok else None
    # 趋势 + MCP 用量
    if ou.get("ok"):
        series = [h.get("tokens", 0) for h in (ou.get("hours") or [])]
        tsrc, ttok, tcall = "官方", ou.get("total_tokens"), ou.get("total_calls")
    else:
        series = [x.get("tokens", 0) for x in (quota.get("trend24h") or [])]
        tsrc, ttok, tcall = "本地", None, None
    return {"cfg": cfg, "kpi": kpi, "quota": quota, "mcp": mcp,
            "official_ok": off_ok, "level": off.get("level"),
            "panel": panel, "series": series, "tsrc": tsrc,
            "ttok": ttok, "tcall": tcall,
            "mcp_month": ou.get("mcp_month") if ou.get("ok") else None,
            "mcp_24h": ou.get("mcp") if ou.get("ok") else None}


# ---------------------------------------------------------------------------
# 主题与绘制工具
# ---------------------------------------------------------------------------
THEMES = {
    "dark": dict(bg="#0b0f14", bg2="#0f141b", card="#131a22", card2="#18212c",
                 border="#232d3a", text="#e6edf3", text2="#aebacb", text3="#7d8b9f",
                 text4="#5b687a", accent="#58a6ff", purple="#bc8cff", green="#3fb950",
                 amber="#e3b341", red="#f85149", track="#0a0e13"),
    "light": dict(bg="#f2f5f9", bg2="#eef1f6", card="#ffffff", card2="#f6f8fa",
                  border="#d8dee8", text="#1c232e", text2="#3d4756", text3="#69758a",
                  text4="#96a0b2", accent="#0969da", purple="#8250df", green="#1a7f37",
                  amber="#9a6700", red="#cf222e", track="#e7ecf3"),
}


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(c):
    return "#%02x%02x%02x" % c


def lerp_color(c1, c2, t):
    a, b = hex2rgb(c1), hex2rgb(c2)
    return rgb2hex(tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))


def fmt_tok(n):
    n = n or 0
    if n >= 1e9:
        return "%.1fG" % (n / 1e9)
    if n >= 1e6:
        return "%dM" % (n / 1e6)
    if n >= 1e3:
        return "%dK" % (n / 1e3)
    return "%d" % n


def fmt_i(n):
    return "—" if n is None else "{:,}".format(int(round(n or 0)))


def fmt_pct(p):
    return "—" if p is None else "%d%%" % round(p * 100)


def cd_str(ms):
    if not ms or ms <= 0:
        return "已重置"
    m = int(ms // 60000)
    h = m // 60
    return ("剩%dh%02d" % (h, m % 60)) if h else ("剩%dm" % m)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
class App:
    W, H, PAD, GAP = 470, 780, 9, 7          # 逻辑尺寸(乘 scale)

    def __init__(self):
        self.scale = 1.0
        try:
            self.scale = max(1.0, _ctypes.windll.user32.GetDpiForSystem() / 96.0)
        except Exception:
            pass
        self.S = lambda v: int(v * self.scale)
        self.theme_name = "dark"
        self.win_key = "today"
        self.cfg = {"refresh_seconds": 10, "theme": "dark", "warn_pct": 85, "crit_pct": 95}
        self.q = queue.Queue(maxsize=4)
        self.last_data = None
        self.refreshing = False
        self._fonts = {}          # Font 对象必须持有引用: 被 GC 后 tk 字体被删,
                                  # Canvas 上已绘制的所有文字项会集体失效消失

        self.root = tk.Tk()
        self.root.title("ZCode 监控")
        # 程序图标(源码运行取脚本目录, 打包后取解包临时目录; 无文件时忽略)
        try:
            _base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            _ico = os.path.join(_base, "logo.ico")
            if os.path.exists(_ico):
                self.root.iconbitmap(_ico)
        except Exception:
            pass
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.w, self.h = self.S(self.W), min(self.S(self.H), sh - self.S(60))
        self._max_h = self.h
        self.root.geometry("%dx%d+%d+%d" % (self.w, self.h,
                                            max(20, sw - self.w - self.S(24)),
                                            self.S(36)))
        self.root.configure(bg=THEMES["dark"]["bg"])

        # 三段式: 顶栏固定 / 中间内容滚动 / 底栏固定
        self.top_cv = tk.Canvas(self.root, height=self.S(40), highlightthickness=0,
                                bd=0, bg=THEMES["dark"]["bg2"])
        self.mid_cv = tk.Canvas(self.root, highlightthickness=0, bd=0,
                                bg=THEMES["dark"]["bg"])
        self.foot_cv = tk.Canvas(self.root, height=self.S(18), highlightthickness=0,
                                 bd=0, bg=THEMES["dark"]["bg2"])
        self.top_cv.pack(side="top", fill="x")
        self.foot_cv.pack(side="bottom", fill="x")
        self.mid_cv.pack(side="top", fill="both", expand=True)
        for _cv in (self.top_cv, self.mid_cv, self.foot_cv):
            _cv.bind("<MouseWheel>", self._on_wheel)
        self.top_cv.bind("<Button-1>", self._on_click)
        self.top_cv.bind("<B1-Motion>", self._on_drag)
        self.top_cv.bind("<ButtonRelease-1>",
                         lambda e: setattr(self, "_drag", None))
        self.root.bind("<Map>", self._on_map)   # 从任务栏还原时恢复无边框
        self._dc = self.mid_cv            # 绘制基元(_rr/_grad_bar)的当前目标

        self._load_cfg()
        self._apply_theme()
        self._draw_skeleton()
        self._kick_refresh()
        self.root.after(250, self._fast_poll)     # 首屏快速轮询, 数据一到立即绘制
        self.root.after(int(self.cfg.get("refresh_seconds", 10)) * 1000,
                        self._poll_timer)

    def _fast_poll(self):
        """首屏加速: 每 250ms 取一次, 拿到数据立即绘制; 之后交给慢速定时器。"""
        self._poll()
        if self.last_data:
            try:
                self._draw(self.last_data)
            except Exception:
                pass
            return                          # 慢速 _poll_timer 接管
        self.root.after(250, self._fast_poll)

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            c = zu.load_monitor_config()
            self.cfg.update({k: c.get(k) for k in
                             ("refresh_seconds", "theme", "warn_pct",
                              "crit_pct", "modules")})
        except Exception:
            pass

    def _apply_theme(self):
        t = self.cfg.get("theme") or "dark"
        self.theme_name = t if t in THEMES else "dark"
        self.T = THEMES[self.theme_name]
        self.root.configure(bg=self.T["bg"])
        self.top_cv.configure(bg=self.T["bg2"])
        self.foot_cv.configure(bg=self.T["bg2"])
        self.mid_cv.configure(bg=self.T["bg"])

    # ---------- 数据 ----------
    def _kick_refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _w_diag(self, tag):
        try:
            with open(os.path.join(os.path.expanduser("~"),
                                   ".zcode-monitor-tk.log"), "a",
                      encoding="utf-8") as f:
                f.write("%s %s\n" % (datetime.datetime.now().isoformat(), tag))
        except Exception:
            pass

    def _worker(self):
        try:
            data = collect_data(self.win_key)
            self.q.put_nowait(("ok", data))
        except Exception as ex:
            try:
                self.q.put_nowait(("err", str(ex)))
            except Exception:
                pass

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    self.last_data = payload
                    if payload.get("cfg"):
                        self.cfg.update({k: payload["cfg"].get(k) for k in
                                         ("refresh_seconds", "theme", "warn_pct", "crit_pct")})
                else:
                    self._err = payload
        except queue.Empty:
            pass

    def _poll_timer(self):
        self._poll()
        if self.last_data:
            try:
                self._draw(self.last_data)
            except Exception:
                import traceback
                try:
                    with open(os.path.join(os.path.expanduser("~"),
                                           ".zcode-monitor-error.log"), "a",
                              encoding="utf-8") as f:
                        f.write("=== draw %s ===\n" % datetime.datetime.now().isoformat())
                        traceback.print_exc(file=f)
                except Exception:
                    pass
        self.refreshing = False
        self._kick_refresh()
        self._timer_id = self.root.after(
            int(self.cfg.get("refresh_seconds", 10)) * 1000, self._poll_timer)

    # ---------- 交互 ----------
    def _on_wheel(self, e):
        self.mid_cv.yview_scroll(int(-e.delta / 120) * 5, "units")

    def _on_click(self, e):
        self._drag = None
        tag = self.top_cv.gettags("current")
        if "btn:close" in tag:
            self.root.destroy()
            return
        if "btn:min" in tag:
            self._minimize()
            return
        if "btn:set" in tag:
            self._open_settings()
            return
        if "btn:theme" in tag:
            self.cfg["theme"] = "light" if self.theme_name == "dark" else "dark"
            try:
                zu.save_override({"theme": self.cfg["theme"]})
            except Exception:
                pass
            self._apply_theme()
            if self.last_data:
                self._draw(self.last_data)
            else:
                self._draw_skeleton()
            return
        for k, _lab in WIN_KEYS:
            if ("win:" + k) in tag:
                if k != self.win_key:
                    self.win_key = k
                    self.last_data = None
                    self._draw_skeleton()
                    self.refreshing = False
                    self._kick_refresh()
                    # 重启快速轮询: 否则新窗数据要等下一个 10s 定时器才上屏
                    self.root.after(250, self._fast_poll)
                return
        # 顶栏空白处(整个顶栏画布): 开始拖拽
        self._drag = (e.x_root, e.y_root)

    def _on_drag(self, e):
        d = getattr(self, "_drag", None)
        if d:
            x = self.root.winfo_x() + e.x_root - d[0]
            y = self.root.winfo_y() + e.y_root - d[1]
            self.root.geometry("+%d+%d" % (x, y))
            self._drag = (e.x_root, e.y_root)

    # ---------- 最小化到任务栏 ----------
    def _minimize(self):
        """无边框窗口没有任务栏按钮: 先临时恢复系统装饰获得任务栏图标,
        再最小化; 从任务栏还原时(<Map>)恢复无边框悬浮形态。"""
        self._saved_geom = self.root.geometry()
        self._minimized = True
        self.root.overrideredirect(False)
        self.root.iconify()

    def _on_map(self, _e=None):
        if not getattr(self, "_minimized", False):
            return
        self._minimized = False
        self.root.overrideredirect(True)
        try:
            self.root.geometry(self._saved_geom)
        except Exception:
            pass
        self.root.attributes("-topmost", True)

    def _open_settings(self):
        w = getattr(self, "_set_win", None)
        if w is not None and w.winfo_exists():
            w.lift(); w.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("ZCode 监控 设置")
        win.configure(bg=self.T["bg2"], padx=14, pady=12)
        win.resizable(False, False)
        win.transient(self.root)
        f = lambda s: tkfont.Font(family="Microsoft YaHei UI", size=self.S(s))
        row = 0

        def lab(txt):
            nonlocal row
            tk.Label(win, text=txt, bg=self.T["bg2"], fg=self.T["text2"],
                     font=f(10)).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 10))
            row += 1

        lab("自动刷新间隔(秒)")
        ref = tk.IntVar(value=int(self.cfg.get("refresh_seconds", 10)))
        tk.Spinbox(win, from_=3, to=300, textvariable=ref, width=6,
                   font=f(10), bg=self.T["card"], fg=self.T["text"],
                   buttonbackground=self.T["card2"],
                   insertbackground=self.T["text"]).grid(row=row - 1, column=1, sticky="w")
        lab("警告阈值(%)")
        wr = tk.IntVar(value=int(self.cfg.get("warn_pct", 85)))
        tk.Spinbox(win, from_=50, to=100, textvariable=wr, width=6, font=f(10),
                   bg=self.T["card"], fg=self.T["text"],
                   buttonbackground=self.T["card2"],
                   insertbackground=self.T["text"]).grid(row=row - 1, column=1, sticky="w")
        lab("危急阈值(%)")
        cr = tk.IntVar(value=int(self.cfg.get("crit_pct", 95)))
        tk.Spinbox(win, from_=50, to=100, textvariable=cr, width=6, font=f(10),
                   bg=self.T["card"], fg=self.T["text"],
                   buttonbackground=self.T["card2"],
                   insertbackground=self.T["text"]).grid(row=row - 1, column=1, sticky="w")
        lab("主题")
        th = tk.StringVar(value=self.theme_name)
        for txt, val in (("深色", "dark"), ("浅色", "light")):
            tk.Radiobutton(win, text=txt, variable=th, value=val, font=f(10),
                           bg=self.T["bg2"], fg=self.T["text"],
                           selectcolor=self.T["card"],
                           activebackground=self.T["bg2"]).grid(
                row=row - 1, column=1 if val == "dark" else 2, sticky="w")
        lab("显示模块")
        MODS = [("kpi", "KPI 总览"), ("quota", "官方额度"), ("dist", "算力分布"),
                ("trend", "趋势图"), ("models", "模型速度")]
        cur_mods = set(self.cfg.get("modules") or
                       ["kpi", "quota", "dist", "trend", "models"])
        mod_vars = {k: tk.BooleanVar(value=k in cur_mods) for k, _ in MODS}
        for ci, (k, txt) in enumerate(MODS):
            tk.Checkbutton(win, text=txt, variable=mod_vars[k], font=f(9),
                           bg=self.T["bg2"], fg=self.T["text"],
                           selectcolor=self.T["card"],
                           activebackground=self.T["bg2"]).grid(
                row=row - 1, column=ci % 3, sticky="w", padx=(0, 4))

        def save():
            mods = [k for k, _ in [("kpi", 1), ("quota", 1), ("dist", 1),
                                   ("trend", 1), ("models", 1)] if mod_vars[k].get()]
            if not mods:
                mods = ["kpi", "quota", "dist", "trend", "models"]
            try:
                zu.save_override({"refresh_seconds": int(ref.get()),
                                  "warn_pct": int(wr.get()), "crit_pct": int(cr.get()),
                                  "theme": th.get(), "modules": ",".join(mods)})
            except Exception:
                pass
            self.cfg.update({"refresh_seconds": int(ref.get()), "warn_pct": int(wr.get()),
                             "crit_pct": int(cr.get()), "theme": th.get(),
                             "modules": mods})
            self._apply_theme()
            win.destroy()
            # 刷新间隔立即生效: 取消已排定时器, 按新间隔重启循环
            if getattr(self, "_timer_id", None):
                try:
                    self.root.after_cancel(self._timer_id)
                except Exception:
                    pass
            self._poll_timer()
            if self.last_data:
                self._draw(self.last_data)
        tk.Button(win, text="保存并应用", command=save, font=f(10),
                  bg=self.T["accent"], fg="#ffffff", bd=0,
                  activebackground=self.T["purple"]).grid(row=row, column=0,
                                                          columnspan=3, sticky="ew", pady=(12, 0))
        win.update_idletasks()
        self._set_win = win
        self.root.after(50, lambda: win.focus_force())

    # ---------- 绘制基元 ----------
    def _rr(self, x1, y1, x2, y2, r=10, **kw):
        """圆角矩形(多边形 smooth 近似)。"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self._dc.create_polygon(pts, smooth=True, **kw)

    def _font(self, size, bold=False):
        key = (size, bool(bold))
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(family="Microsoft YaHei UI",
                                           size=self.S(size),
                                           weight="bold" if bold else "normal")
        return self._fonts[key]

    def _grad_bar(self, x1, y, x2, h, ratio, warn, crit):
        """渐变进度条: 健康=蓝→紫; >=warn 琥珀; >=crit 红。"""
        T = self.T
        self._dc.create_rectangle(x1, y, x2, y + h, fill=T["track"],
                                  outline=T["border"])
        if not ratio or ratio <= 0:
            return
        ratio = min(ratio, 1.0)
        if ratio >= crit / 100.0:
            color1 = color2 = T["red"]
        elif ratio >= warn / 100.0:
            color1 = color2 = T["amber"]
        else:
            color1, color2 = T["accent"], T["purple"]
        fw = max(1, int((x2 - x1 - 2) * ratio))
        segs = 20
        for i in range(segs):
            c = lerp_color(color1, color2, i / max(1, segs - 1))
            sx = x1 + 1 + fw * i // segs
            ex = x1 + 1 + fw * (i + 1) // segs
            if ex > sx:
                self._dc.create_rectangle(sx, y + 1, ex, y + h - 1,
                                          fill=c, outline=c)

    def _pc(self, p):
        """按阈值给数值着色。"""
        if p is None:
            return self.T["text3"]
        if p >= self.cfg.get("crit_pct", 95) / 100.0:
            return self.T["red"]
        if p >= self.cfg.get("warn_pct", 85) / 100.0:
            return self.T["amber"]
        return self.T["green"]

    # ---------- 各板块 ----------
    def _draw_skeleton(self):
        self.mid_cv.delete("all")
        self.foot_cv.delete("all")
        self._draw_topbar()
        self.mid_cv.create_text(self.w // 2, self.S(90),
                                text="正在读取本地数据库与官方额度…",
                                fill=self.T["text3"], font=self._font(10))

    def _draw_topbar(self):
        c, T = self.top_cv, self.T
        c.delete("all")
        self._dc = c
        H = self.S(40)
        cy = H // 2
        self._rr(0, 0, self.w, H, 0, fill=T["bg2"], outline="")
        c.create_line(0, H, self.w, H, fill=T["border"])
        # 状态点 + 标题(左)
        r = self.S(3)
        c.create_oval(self.S(12), cy - r, self.S(12) + 2 * r, cy + r,
                     fill=T["green"], outline=T["green"])
        c.create_text(self.S(22), cy, anchor="w", text="ZCode 监控",
                      fill=T["text"], font=self._font(11, True))
        # 右侧图标区(从右往左 ✕ — ⚙ ◐, 各占 28 逻辑宽, 附加大点击热区)
        for i, (tag, txt) in enumerate((("btn:close", "✕"),
                                        ("btn:min", "—"),
                                        ("btn:set", "⚙"),
                                        ("btn:theme", "◐"))):
            bx = self.w - self.S(16) - i * self.S(28)
            c.create_rectangle(bx - self.S(13), cy - self.S(13),
                               bx + self.S(13), cy + self.S(13),
                               fill=T["bg2"], outline="", tags=(tag,))
            c.create_text(bx, cy, text=txt,
                          fill=T["red"] if tag == "btn:close" else T["text3"],
                          font=self._font(11, True), tags=(tag,))
        # 时间窗按钮组: 右对齐于图标区左侧, 整个按钮(polygon+文字)都可点击
        bw, gap = self.S(42), self.S(4)
        x_end = self.w - self.S(16) - 4 * self.S(28) - self.S(10)
        x = x_end - (len(WIN_KEYS) * bw + (len(WIN_KEYS) - 1) * gap)
        for k, lab in WIN_KEYS:
            on = (k == self.win_key)
            self._rr(x, self.S(8), x + bw, self.S(32), self.S(6),
                     fill=T["card"] if on else T["card2"],
                     outline=T["accent"] if on else T["border"],
                     tags=("win:" + k,))
            c.create_text(x + bw // 2, cy, text=lab,
                          fill=T["text"] if on else T["text3"],
                          font=self._font(9, True), tags=("win:" + k,))
            x += bw + gap
        self._dc = self.mid_cv            # 绘制目标切回内容区

    def _card(self, y, h):
        T = self.T
        x1, x2 = self.S(self.PAD), self.w - self.S(self.PAD)
        self._rr(x1, y, x2, y + h, self.S(9), fill=T["card"], outline=T["border"])
        return x1, x2

    def _draw(self, d):
        c = self.mid_cv
        c.delete("all")
        self._draw_topbar()
        T = self.T
        warn, crit = self.cfg.get("warn_pct", 85), self.cfg.get("crit_pct", 95)
        kpi, quota, panel = d["kpi"], d["quota"], d["panel"]
        blk, wk = quota.get("blk") or {}, quota.get("wk") or {}
        ctx = quota.get("context") or None
        mcp = d.get("mcp")
        mods = set(self.cfg.get("modules") or
                   ["kpi", "quota", "dist", "trend", "models"])
        y = self.S(self.GAP)

        # --- KPI 行 ---
        if "kpi" in mods:
            kh = self.S(60)
            kw = (self.w - self.S(self.PAD) * 2 - self.S(6) * 3) // 4
            items = [("有效请求", fmt_i(kpi.get("completed")), T["text"]),
                     ("输入 TOK", fmt_tok(kpi.get("input_tokens")), T["accent"]),
                     ("输出 TOK", fmt_tok((kpi.get("output_tokens") or 0) +
                                          (kpi.get("reasoning_tokens") or 0)), T["green"]),
                     ("活跃会话", fmt_i(kpi.get("active_sessions")), T["purple"])]
            for i, (lab, val, col) in enumerate(items):
                x = self.S(self.PAD) + i * (kw + self.S(6))
                self._rr(x, y, x + kw, y + kh, self.S(7), fill=T["card"],
                         outline=T["border"])
                c.create_text(x + kw // 2, y + self.S(15), text=lab, fill=T["text3"],
                              font=self._font(8))
                c.create_text(x + kw // 2, y + self.S(41), text=val, fill=col,
                              font=self._font(10, True))
            y += kh + self.S(self.GAP)

        # --- 额度卡 ---
        pct, wpct = blk.get("pct"), wk.get("pct")
        if "quota" in mods:
            peak = bool(quota.get("is_peak"))
            cd = cd_str((blk.get("reset_ms") or 0) - (quota.get("now") or 0)
                        if blk.get("reset_ms") else None)
            # 主行: 5h积分 + 速率/外推同一行(积分在前); 副行: 本周/上下文/MCP
            main = "%s / %s 积分   余 %s" % (fmt_i(blk.get("used")),
                                             fmt_i(blk.get("quota")),
                                             fmt_i(blk.get("remaining")))
            tail = "速率 %s/h" % fmt_tok(blk.get("burn_tok_h"))
            if blk.get("projected_pct") is not None:
                tail += " · 外推 %s" % fmt_pct(blk.get("projected_pct"))
            row2 = "本周 %s / %s (%s)" % (fmt_i(wk.get("used")),
                                          fmt_i(wk.get("quota")), fmt_pct(wpct))
            if ctx and ctx.get("limit"):
                row2 += "   ·   上下文 %s / %s" % (fmt_tok(ctx.get("input_tokens")),
                                                   fmt_tok(ctx.get("limit")))
            row3 = ""
            if mcp and mcp.get("quota"):
                row3 = "MCP月 %s / %s" % (fmt_i(mcp.get("used")),
                                          fmt_i(mcp.get("quota")))
            ch = self.S(114) if row3 else self.S(96)
            self._card(y, ch)
            ix = self.S(self.PAD) + self.S(10)
            c.create_text(ix, y + self.S(14), anchor="w",
                          text=("额度 · 官方 " + str(d.get("level") or "").upper())
                          if d.get("official_ok") else "额度(本地估算)",
                          fill=T["text"], font=self._font(10, True))
            c.create_text(ix + self.S(134), y + self.S(14), anchor="w",
                          text="高峰·全额" if peak else "非高峰·5折",
                          fill=T["amber"] if peak else T["green"], font=self._font(8, True))
            c.create_text(self.w - self.S(self.PAD) - self.S(10), y + self.S(14), anchor="e",
                          text=cd, fill=T["text2"], font=self._font(9, True))
            main = "%s / %s 积分   余 %s" % (fmt_i(blk.get("used")),
                                             fmt_i(blk.get("quota")),
                                             fmt_i(blk.get("remaining")))
            main_id = c.create_text(ix, y + self.S(41), anchor="w", text=main,
                                    fill=T["text"], font=self._font(8, True))
            pct_id = c.create_text(self.w - self.S(self.PAD) - self.S(10),
                                   y + self.S(41), anchor="e", text=fmt_pct(pct),
                                   fill=self._pc(pct), font=self._font(10, True))
            mb, pb = c.bbox(main_id), c.bbox(pct_id)
            tail_x = (mb[2] if mb else ix + self.S(200)) + self.S(8)
            tail_room = (pb[0] if pb else self.w) - self.S(8) - tail_x
            t_text = tail
            if self._font(8).measure(tail) > tail_room:
                t_text = tail.split("   ·")[0]           # 超宽则只留速率, 舍外推
            if t_text and self._font(8).measure(t_text) > tail_room:
                t_text = ""                              # 连速率都放不下则不画
            if t_text:
                c.create_text(tail_x, y + self.S(41), anchor="w", text=t_text,
                              fill=T["text3"], font=self._font(8))
            self._grad_bar(ix, y + self.S(56), self.w - self.S(self.PAD) - self.S(10),
                           self.S(6), pct, warn, crit)
            c.create_text(ix, y + self.S(80), anchor="w", text=row2,
                          fill=T["text3"], font=self._font(8))
            if row3:
                c.create_text(ix, y + self.S(100), anchor="w", text=row3,
                              fill=T["text3"], font=self._font(8))
            y += ch + self.S(self.GAP)

        # --- 算力分布 ---
        models = sorted(panel.items(), key=lambda kv: -kv[1].get("total_tokens", 0))
        if models and "dist" in mods:
            mains = sum(m.get("main_count", 0) for _, m in models)
            subs = sum(m.get("subagent_count", 0) for _, m in models)
            calls = mains + subs
            total = sum(m.get("total_tokens", 0) for _, m in models) or 1
            rows = models[:6]
            dh = self.S(33) + self.S(21) * len(rows)
            self._card(y, dh)
            ix = self.S(self.PAD) + self.S(10)
            c.create_text(ix, y + self.S(14), anchor="w", text="算力分布",
                          fill=T["text3"], font=self._font(9, True))
            if calls:
                c.create_text(self.w - self.S(self.PAD) - self.S(10), y + self.S(14),
                              anchor="e", text="主 %d%% · 子 %d%%" %
                              (round(mains * 100 / calls), round(subs * 100 / calls)),
                              fill=T["text4"], font=self._font(8))
            palette = [T["accent"], T["purple"], "#39c5cf", T["green"], T["amber"], T["red"]]
            ry = y + self.S(31)
            for i, (name, m) in enumerate(rows):
                r = m.get("total_tokens", 0) / total
                c.create_text(ix, ry + self.S(8), anchor="w",
                              text=name if len(name) <= 20 else name[:19] + "…",
                              fill=T["text2"], font=self._font(8))
                bx1, bx2 = ix + self.S(150), self.w - self.S(self.PAD) - self.S(88)
                c.create_rectangle(bx1, ry + self.S(3), bx2, ry + self.S(11),
                                   fill=T["track"], outline="")
                c.create_rectangle(bx1, ry + self.S(3),
                                   bx1 + max(1, int((bx2 - bx1) * min(r, 1))),
                                   ry + self.S(11), fill=palette[i % 6],
                                   outline=palette[i % 6])
                c.create_text(self.w - self.S(self.PAD) - self.S(10), ry + self.S(8),
                              anchor="e", text="%d%% %s" % (round(r * 100),
                                                            fmt_tok(m.get("total_tokens"))),
                              fill=T["text3"], font=self._font(8))
                ry += self.S(22)
            y += dh + self.S(self.GAP)

        # --- 24h 趋势(含 MCP 行) ---
        series = d.get("series") or []
        if "trend" in mods:
            mm, m24 = d.get("mcp_month") or {}, d.get("mcp_24h") or {}
            has_mcp = bool(mm or m24)
            th_ = self.S(93) + (self.S(17) if has_mcp else 0)
            self._card(y, th_)
            ix = self.S(self.PAD) + self.S(10)
            c.create_text(ix, y + self.S(14), anchor="w",
                          text="24h 趋势(%s)" % d.get("tsrc", "本地"),
                          fill=T["text3"], font=self._font(9, True))
            peak_txt = "峰 %s" % fmt_tok(max(series) if series else 0)
            if d.get("ttok") is not None:
                peak_txt += " · 总 %s · %s次" % (fmt_tok(d["ttok"]), fmt_i(d.get("tcall")))
            c.create_text(self.w - self.S(self.PAD) - self.S(10), y + self.S(14), anchor="e",
                          text=peak_txt, fill=T["text4"], font=self._font(8))
            if len(series) >= 2:
                gx1, gx2 = ix, self.w - self.S(self.PAD) - self.S(10)
                gy1 = y + self.S(28)
                gy2 = y + th_ - (self.S(26) if has_mcp else self.S(8))
                mx = max(series) or 1
                pts = []
                n = len(series)
                for i, v in enumerate(series):
                    px = gx1 + (gx2 - gx1) * i / (n - 1)
                    py = gy2 - (gy2 - gy1) * (v / mx)
                    pts.extend((px, py))
                c.create_polygon(gx1, gy2, *pts, gx2, gy2, fill=T["card2"], outline="")
                c.create_line(*pts, fill=T["accent"], width=self.S(1.4), smooth=True)
            if has_mcp:
                mtxt = "MCP 本月"
                if mm.get("search") is not None:
                    mtxt += " 搜%s" % fmt_i(mm.get("search"))
                if mm.get("web_read") is not None:
                    mtxt += " 读%s" % fmt_i(mm.get("web_read"))
                s24 = (m24.get("search") or 0) + (m24.get("web_read") or 0)
                if s24:
                    mtxt += "  ·  24h 搜%d·读%d" % (m24.get("search") or 0,
                                                     m24.get("web_read") or 0)
                c.create_text(ix, y + th_ - self.S(9), anchor="w", text=mtxt,
                              fill=T["text3"], font=self._font(8))
            y += th_ + self.S(self.GAP)

        # --- 模型速度 ---
        if models and "models" in mods:
            mh = self.S(29) + self.S(144) * len(models)
            self._card(y, mh)
            ix = self.S(self.PAD) + self.S(10)
            c.create_text(ix, y + self.S(13), anchor="w", text="模型速度",
                          fill=T["text3"], font=self._font(9, True))
            my = y + self.S(25)
            for name, m in models:
                tps = m.get("weighted_tps")
                col = T["green"] if (tps or 0) >= 80 else (T["amber"] if (tps or 0) >= 30 else T["red"])
                c.create_text(ix, my + self.S(12), anchor="w",
                              text=name if len(name) <= 22 else name[:21] + "…",
                              fill=T["accent"], font=self._font(9, True))
                c.create_text(self.w - self.S(self.PAD) - self.S(96), my + self.S(12),
                              anchor="e", text="%s tok/s" % fmt_i_round1(tps), fill=col,
                              font=self._font(9, True))
                c.create_text(self.w - self.S(self.PAD) - self.S(10), my + self.S(12),
                              anchor="e", text="请求 %s" % fmt_i(m.get("requests")),
                              fill=T["text4"], font=self._font(8))
                sp = m.get("speed_tok_s") or {}
                tt = m.get("ttft_s") or {}
                du = m.get("duration_s") or {}
                ot = m.get("output_tokens") or {}
                it = m.get("input_tokens") or {}
                sub = [("速度", "中 %s · P90 %s · 快 %s" %
                        (f1(sp.get("median")), f1(sp.get("p90")), f1(sp.get("max")))),
                       ("首token", "均 %ss · 中 %ss · P90 %ss" %
                        (f1(tt.get("avg")), f1(tt.get("median")), f1(tt.get("p90")))),
                       ("耗时", "均 %ss · 中 %ss · P90 %ss" %
                        (f1(du.get("avg")), f1(du.get("median")), f1(du.get("p90")))),
                       ("输出", "均 %s · 大 %s" % (fmt_tok(ot.get("avg")), fmt_tok(ot.get("max")))),
                       ("输入", "均 %s · 大 %s" % (fmt_tok(it.get("avg")), fmt_tok(it.get("max"))))]
                sy = my + self.S(36)
                for lab, val in sub:
                    c.create_text(ix, sy, anchor="w", text=lab, fill=T["text4"],
                                  font=self._font(8))
                    c.create_text(ix + self.S(52), sy, anchor="w", text=val,
                                  fill=T["text3"], font=self._font(8))
                    sy += self.S(21)
                c.create_line(ix, my + self.S(138),
                              self.w - self.S(self.PAD) - self.S(10),
                              my + self.S(138), fill=T["border"])
                my += self.S(144)
            y += mh + self.S(self.GAP)

        # --- 底部状态(独立固定画布, 不随内容滚动) ---
        fc = self.foot_cv
        fc.delete("all")
        err = getattr(self, "_err", None)
        st = "刷新 %s · %s · %d 模型%s · %ds" % (
            datetime.datetime.now().strftime("%H:%M:%S"),
            dict(WIN_KEYS)[self.win_key], len(models),
            (" · 错误:%s" % err[:24]) if err else "",
            int(self.cfg.get("refresh_seconds", 10)))
        fc.create_text(self.S(10), self.S(9), anchor="w", text=st,
                       fill=T["text4"], font=self._font(7))
        # 内容区滚动范围与窗口高度自适应(顶栏40 + 内容 + 底栏18)
        content_h = y
        self.mid_cv.configure(scrollregion=(0, 0, self.w, content_h))
        want = min(self._max_h,
                   self.S(40) + content_h + self.S(18))
        if abs(want - self.h) > self.S(6):
            self.h = want
            try:
                self.root.geometry("%dx%d+%d+%d" % (self.w, self.h,
                                                    self.root.winfo_x(),
                                                    self.root.winfo_y()))
            except Exception:
                pass


def f1(n):
    return "—" if n is None else "%.1f" % n


def fmt_i_round1(n):
    return "—" if n is None else "%.1f" % n


def _focus_existing():
    """单实例: 已有同标题窗口则激活并返回 True。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            if u.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(128)
                u.GetWindowTextW(hwnd, buf, 128)
                if buf.value == "ZCode 监控":
                    found.append(hwnd)
                    return False
            return True
        u.EnumWindows(cb, 0)
        if found:
            u.ShowWindow(found[0], 9)
            u.SetForegroundWindow(found[0])
            return True
    except Exception:
        pass
    return False


def main():
    if _focus_existing():
        return
    App().root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(os.path.join(os.path.expanduser("~"),
                                   ".zcode-monitor-error.log"), "a",
                      encoding="utf-8") as f:
                f.write("=== %s ===\n" % datetime.datetime.now().isoformat())
                traceback.print_exc(file=f)
        finally:
            raise
