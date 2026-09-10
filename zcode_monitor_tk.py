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


def _fetch_recent(limit=15):
    """最近请求日志(时间倒序): 时间/模型/状态/耗时/输入输出/缓存读。"""
    try:
        con, _ = zu.open_usage_db()
        try:
            rows = con.execute(
                "SELECT started_at, model_id, status, duration_ms, "
                "input_tokens, output_tokens, reasoning_tokens, "
                "cache_read_input_tokens "
                "FROM model_usage ORDER BY started_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        finally:
            con.close()
        return [{"ts": r[0] or 0, "model": r[1] or "?", "status": r[2] or "?",
                 "dur_ms": r[3] or 0, "in_tok": r[4] or 0,
                 "out_tok": (r[5] or 0) + (r[6] or 0),
                 "cread": r[7] or 0} for r in rows]
    except Exception:
        return []


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
            "mcp_24h": ou.get("mcp") if ou.get("ok") else None,
            "recent": _fetch_recent(50)}


# ---------------------------------------------------------------------------
# 主题与绘制工具
# ---------------------------------------------------------------------------
THEMES = {
    "dark": dict(bg="#0b0f14", bg2="#0f141b", card="#131a22", card2="#18212c",
                 border="#232d3a", border2="#3a4a5e", text="#e6edf3", text2="#aebacb", text3="#7d8b9f",
                 text4="#5b687a", accent="#58a6ff", cyan="#39c5cf", purple="#bc8cff",
                 green="#3fb950", amber="#e3b341", red="#f85149", track="#0a0e13"),
    "light": dict(bg="#f2f5f9", bg2="#eef1f6", card="#ffffff", card2="#f6f8fa",
                  border="#d8dee8", border2="#b8c4d4", text="#1c232e", text2="#3d4756", text3="#69758a",
                  text4="#96a0b2", accent="#0969da", cyan="#0e7490", purple="#8250df",
                  green="#1a7f37", amber="#9a6700", red="#cf222e", track="#e7ecf3"),
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
    _LOGW_W = 600                            # 请求日志悬浮窗宽度(逻辑; 不常驻, 可宽)

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
        # 工作区(扣除任务栏)底边: 窗口上限用它, 防止长窗口盖住任务栏
        wa_bottom = sh
        try:
            class _RECT(_ctypes.Structure):
                _fields_ = [("l", _ctypes.c_long), ("t", _ctypes.c_long),
                            ("r", _ctypes.c_long), ("b", _ctypes.c_long)]
            _rc = _RECT()
            if _ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, _ctypes.byref(_rc), 0) and _rc.b > 0:
                wa_bottom = _rc.b          # SPI_GETWORKAREA(物理px, 已扣任务栏)
        except Exception:
            pass
        self._sh = wa_bottom                   # 可用底边(工作区)
        hard_cap = wa_bottom - self.S(8)       # 硬上限: 工作区高
        # 窗口位置 + 自定义高度上限: 均读注册表(优先), 无则默认
        try:
            _pc = zu.load_monitor_config()
            _px, _py = _pc.get("win_x"), _pc.get("win_y")
            _wh = int(_pc.get("win_h") or 0)
        except Exception:
            _px, _py, _wh = None, None, 0
        # 高度上限: 自定义(win_h 逻辑px; 0=自动) → 模块完整展开,
        # 大面板不滚动, 溢出部分由模型卡内部滚动吸收
        self._custom_h = _wh if _wh > 0 else 0
        self._max_h = min(self.S(_wh) if _wh > 0 else hard_cap, hard_cap)
        self._max_h_eff = self._max_h     # 每帧有效上限(自定义过小时自动补足)
        self.w, self.h = self.S(self.W), min(self.S(self.H), self._max_h)
        if _px is None or _py is None:
            _px = max(self.S(20), sw - self.w - self.S(24))
            _py = self.S(36)
        _px = max(0, min(int(_px), sw - self.S(80)))
        _py = max(0, min(int(_py), sh - self.S(80)))
        self.root.geometry("%dx%d+%d+%d" % (self.w, self.h, _px, _py))
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
        self.top_cv.bind("<ButtonRelease-1>", self._on_drag_end)
        # 底栏: 拖动调节窗口高度(光标提示可拖; 双击恢复自动高度)
        self.foot_cv.configure(cursor="sb_v_double_arrow")
        self.foot_cv.bind("<Button-1>", self._on_resize_start)
        self.foot_cv.bind("<B1-Motion>", self._on_resize_motion)
        self.foot_cv.bind("<ButtonRelease-1>", self._on_resize_end)
        self.foot_cv.bind("<Double-Button-1>", self._on_resize_reset)
        self.root.bind("<Map>", self._on_map)   # 从任务栏还原时恢复无边框
        self._dc = self.mid_cv            # 绘制基元(_rr/_grad_bar)的当前目标

        # 模型速度卡内嵌独立滚动区(只创建一次, 每次绘制复用):
        # 卡内滚轮只滚卡片内容, 不影响主内容区; 右侧细滚动条可拖动
        self._sb_wpx = self.S(7)
        self._models_win_id = None
        self._models_view_h = 1
        self._models_content_h = 1
        self._models_cv_w = 1
        self._sb_thumb_h = 0
        self._sb_drag = False
        self._log_win = None              # 请求日志悬浮窗
        self._log_sb = None
        self._log_view_h = 1
        self._log_content_h = 1
        self._log_thumb_y = -1
        self._log_thumb_h = 0
        self._log_sb_drag = False
        self._log_sb_dy = 0
        self._tip_win = None              # 模型名悬浮提示
        self._tip_name = None
        self._mname_map = {}              # 模型名文字项 -> 全名(悬浮提示用)
        self._warn_lv = {"block": 0, "week": 0}   # 额度预警等级(0正常/1警告/2危急)
        self._err = None                  # 最近一次数据错误
        self._drag_moved = False          # 拖动是否实际移动过(决定是否保存位置)
        self._resize_drag = None          # 底栏拖动调高: (起始y, 起始窗口高)
        self._resize_moved = False        # 底栏拖动是否真实移动过(单击不算)
        self._full_need_h = 0             # 全部内容展开所需窗口高(拖动上限)
        self._minimized = False
        self._timer_id = None
        self._fast_id = None
        self._fast_gen = 0                # 快速轮询链代数(防重复链叠加)
        self._refresh_seq = 0             # 刷新请求序号(防迟到旧数据覆盖新数据)
        self._applied_seq = 0
        self._wheel_acc = {"main": 0.0, "models": 0.0, "log": 0.0}  # 滚轮精细增量累加器
        self.models_wrap = tk.Frame(self.mid_cv, bg=THEMES["dark"]["card"],
                                    highlightthickness=0, bd=0)
        self.models_sb = tk.Canvas(self.models_wrap, width=self._sb_wpx,
                                   highlightthickness=0, bd=0,
                                   bg=THEMES["dark"]["card"])
        self.models_sb.pack(side="right", fill="y")
        self.models_cv = tk.Canvas(self.models_wrap, highlightthickness=0, bd=0,
                                   bg=THEMES["dark"]["card"])
        self.models_cv.pack(side="left", fill="both", expand=True)
        self.models_cv.configure(yscrollincrement=1)   # 1像素/单位 → 像素级丝滑滚动
        self.models_cv.bind("<MouseWheel>", self._on_models_wheel)
        self.models_wrap.bind("<MouseWheel>", self._on_models_wheel)
        self.models_sb.bind("<MouseWheel>", self._on_models_wheel)
        self.models_cv.bind("<Motion>", self._on_models_motion)
        self.models_cv.bind("<Leave>", lambda _e: self._hide_tip())
        self.models_sb.bind("<Button-1>", self._on_sb_press)
        self.models_sb.bind("<B1-Motion>", self._on_sb_drag)
        self.models_sb.bind("<ButtonRelease-1>", self._on_sb_release)
        self.mid_cv.bind("<Button-1>", self._on_mid_click)

        self._load_cfg()
        self._apply_theme()
        self._draw_skeleton()
        self._kick_refresh()
        self._start_fast_poll()                                # 首屏快速轮询
        self._timer_id = self.root.after(                      # 记录ID, 最小化可取消
            int(self.cfg.get("refresh_seconds", 10)) * 1000, self._poll_timer)

    def _start_fast_poll(self):
        """启动(或重启)快速轮询链。首屏/切窗共用; 重复调用自动废弃旧链,
        避免快速切窗时多条链并行叠加(高频重复取数)。"""
        self._fast_gen += 1
        if getattr(self, "_fast_id", None):
            try:
                self.root.after_cancel(self._fast_id)
            except Exception:
                pass
            self._fast_id = None
        gen = self._fast_gen
        self._fast_id = self.root.after(250, lambda: self._fast_poll(gen))

    def _fast_poll(self, gen):
        """首屏/切窗加速: 每 250ms 取一次, 拿到数据立即绘制; 之后交给慢速定时器。"""
        self._fast_id = None
        if gen != self._fast_gen:
            return                          # 过期链(又切窗了): 停止
        if getattr(self, "_minimized", False):
            return                          # 最小化: 不再轮询
        self._poll()
        if self.last_data:
            try:
                self._draw(self.last_data)
                self._check_warn(self.last_data.get("quota") or {})
            except Exception:
                pass
            return                          # 慢速 _poll_timer 接管
        self._fast_id = self.root.after(250, lambda: self._fast_poll(gen))

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            c = zu.load_monitor_config()
            self.cfg.update({k: c.get(k) for k in
                             ("refresh_seconds", "theme", "warn_pct",
                              "crit_pct", "modules", "log_open",
                              "win_x", "win_y", "win_h")})
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
        for _w in (self.models_wrap, self.models_cv, self.models_sb):
            _w.configure(bg=self.T["card"])
        # 请求日志悬浮窗(若已打开)同步主题
        w = getattr(self, "_log_win", None)
        if w is not None and w.winfo_exists():
            try:
                w.configure(bg=self.T["bg2"])
                for _c in ("_log_cv", "_log_hdr", "_log_sb"):
                    _cv2 = getattr(self, _c, None)
                    if _cv2 is not None and _cv2.winfo_exists():
                        _cv2.configure(bg=self.T["bg2"])
                self._draw_log_window()
            except Exception:
                pass

    # ---------- 数据 ----------
    def _kick_refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        key = self.win_key                     # 快照: 数据须与该窗口匹配
        self._refresh_seq += 1                 # 序号: 防乱序(慢的先发后到)
        seq = self._refresh_seq
        threading.Thread(target=self._worker, args=(key, seq), daemon=True).start()

    def _worker(self, key, seq):
        try:
            data = collect_data(key)
            self.q.put_nowait(("ok", data, key, seq))
        except Exception as ex:
            try:
                self.q.put_nowait(("err", str(ex), key, seq))
            except Exception:
                pass

    def _poll(self):
        """取后台数据: 只采纳「当前窗口 + 比已应用更新」的结果,
        切窗或乱序迟到的旧数据一律丢弃。"""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                kind, payload, key, seq = item
            except (TypeError, ValueError):
                continue                       # 防御: 异常结构直接丢弃
            if key != self.win_key:
                continue                       # 切窗后迟到的旧窗口数据
            if kind == "ok":
                if seq <= self._applied_seq:
                    continue                   # 乱序: 已应用更新的数据
                self._applied_seq = seq
                self.last_data = payload
                self._err = None               # 新数据到达: 清除错误提示
                if payload.get("cfg"):
                    self.cfg.update({k: payload["cfg"].get(k) for k in
                                     ("refresh_seconds", "theme", "warn_pct", "crit_pct")})
            else:
                self._err = payload

    def _poll_timer(self):
        self._poll()
        if self.last_data:
            try:
                self._draw(self.last_data)
                self._check_warn(self.last_data.get("quota") or {})
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

    # ---------- 额度预警(标题红点) ----------
    def _check_warn(self, quota):
        """额度越过警告/危急阈值时, 顶栏标题旁显示红点(危急=红/警告=琥珀)。
        红点是常驻状态显示(非打扰性), 恢复正常后自动消失。"""
        def lv_of(p):
            if p is None:
                return 0
            if p >= self.cfg.get("crit_pct", 95) / 100.0:
                return 2
            if p >= self.cfg.get("warn_pct", 85) / 100.0:
                return 1
            return 0
        blk = quota.get("blk") or quota.get("block") or {}
        wk = quota.get("wk") or quota.get("week") or {}
        cur = {"block": lv_of(blk.get("pct")), "week": lv_of(wk.get("pct"))}
        if cur != self._warn_lv:
            self._warn_lv = cur
            self._draw_topbar()          # 等级变化 → 重绘顶栏红点

    # ---------- 交互 ----------
    def _wheel_scroll(self, cv, key, delta, per_notch):
        """统一滚轮处理: 鼠标一格 delta=±120; 触摸板小增量按比例累加,
        避免 int(delta/120) 把小于一格的量截断为 0(触摸板滚不动)。
        返回本次滚动的单位数。"""
        acc = self._wheel_acc.get(key, 0.0) + (delta / 120.0) * per_notch
        n = int(acc)                       # 取整, 余量留下次累加
        self._wheel_acc[key] = acc - n
        if n:
            cv.yview_scroll(-n, "units")
        return n

    def _on_wheel(self, e):
        self._wheel_scroll(self.mid_cv, "main", e.delta, 5)
        return "break"

    def _on_models_wheel(self, e):
        """模型速度卡内滚轮: 只滚卡内内容, 不影响主内容区;
        卡内未溢出时滚轮仍作用于主内容区。像素级滚动(50px/格)。"""
        vh, ch = self._models_view_bounds()
        if ch > vh + 2:
            self._wheel_scroll(self.models_cv, "models", e.delta, 50)
            self._sync_models_sb()
        else:
            self._wheel_scroll(self.mid_cv, "main", e.delta, 5)
        return "break"                      # 阻止冒泡到主画布

    def _on_models_motion(self, e):
        """悬停模型名(被截断时)弹出全名提示。
        注意: 卡内滚动后 e.x/e.y 是控件坐标, 必须先 canvasx/y 转画布坐标,
        否则查不到(或查到错误)元素。"""
        try:
            cx, cy = self.models_cv.canvasx(e.x), self.models_cv.canvasy(e.y)
            items = self.models_cv.find_overlapping(cx - 1, cy - 1,
                                                    cx + 1, cy + 1)
        except Exception:
            items = ()
        full = None
        for i in items:
            if i in self._mname_map:
                full = self._mname_map[i]
                break
        if full:
            self._show_tip(full, e.x_root, e.y_root)
        else:
            self._hide_tip()

    def _show_tip(self, text, x_root, y_root):
        """模型全名小提示窗(无边框置顶, 跟随鼠标上方)。"""
        if getattr(self, "_tip_name", None) == text:
            return                              # 同名已在显示
        self._hide_tip()
        try:
            tip = tk.Toplevel(self.root)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            f = self._font(8)
            tw = f.measure(text) + self.S(16)
            th = self.S(22)
            tip.geometry("%dx%d+%d+%d" % (tw, th, x_root + self.S(10),
                                          max(0, y_root - th - self.S(6))))
            cv = tk.Canvas(tip, highlightthickness=0, bd=0, bg=self.T["card2"])
            cv.pack(fill="both", expand=True)
            cv.create_text(tw // 2, th // 2, text=text, fill=self.T["text"],
                           font=f)
            self._tip_win = tip
            self._tip_name = text
        except Exception:
            pass

    def _hide_tip(self):
        tip = getattr(self, "_tip_win", None)
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
        self._tip_win = None
        self._tip_name = None

    def _models_view_bounds(self):
        """卡内滚动: 可视高度/内容高度(像素, 用绘制时记录的可靠值)。"""
        return max(self._models_view_h, 1), max(self._models_content_h, 1)

    def _sync_models_sb(self):
        """按卡内滚动位置重画右侧细滚动条(用自身尺寸计算, 不依赖 yview 时序)。"""
        if self._models_win_id is None:
            return
        sb = self.models_sb
        sb.delete("all")
        vh, ch = self._models_view_bounds()
        if ch <= vh + 2:
            self._sb_thumb_h = 0
            return
        w = self._sb_wpx
        track_h = max(vh, 1)
        frac = min(1.0, float(vh) / float(ch))
        th = max(self.S(18), int(track_h * frac))
        first, _last = self.models_cv.yview()
        ty = max(0, min(track_h - th, int(first * track_h)))
        self._sb_thumb_h = th
        self._sb_thumb_y = ty
        sb.create_rectangle(0, 0, w, track_h, fill=self.T["track"], outline="")
        sb.create_rectangle(1, ty + 1, w - 1, ty + th - 1,
                            fill=self.T["border2"], outline="")

    def _on_sb_press(self, e):
        vh, ch = self._models_view_bounds()
        if ch <= vh + 2:
            return
        # 点轨道空白: 翻页; 点滑块: 开始拖动
        if getattr(self, "_sb_thumb_y", -1) <= e.y <= getattr(self, "_sb_thumb_y", -1) + self._sb_thumb_h:
            self._sb_drag = True
            self._sb_drag_dy = e.y - self._sb_thumb_y
        else:
            self.models_cv.yview_moveto(max(0.0, min(1.0, e.y / max(vh, 1))))
            self._sync_models_sb()

    def _on_sb_drag(self, e):
        if not getattr(self, "_sb_drag", False):
            return
        vh, ch = self._models_view_bounds()
        track_h = max(vh, 1)
        th = max(self.S(18), int(track_h * vh / max(ch, 1)))
        ty = max(0, min(track_h - th, e.y - getattr(self, "_sb_drag_dy", 0)))
        self.models_cv.yview_moveto(ty / track_h)
        self._sync_models_sb()

    def _on_sb_release(self, _e):
        self._sb_drag = False

    def _on_mid_click(self, e):
        """主内容区点击: 命中「请求日志」按钮时弹出独立日志悬浮窗。"""
        tag = self.mid_cv.gettags("current")
        if "log:open" in tag:
            self._open_log_window()
            return

    # ---------- 请求日志悬浮窗 ----------
    def _open_log_window(self):
        """独立请求日志悬浮窗: 表头固定 + 数据区可滚动(50 条);
        不点关闭则一直显示, 随主程序关闭。"""
        w = getattr(self, "_log_win", None)
        if w is not None and w.winfo_exists():
            w.deiconify()
            w.lift()
            w.attributes("-topmost", True)
            self._draw_log_window()
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        lw = self.S(self._LOGW_W)
        self._log_lw = lw
        HEAD_H = self.S(30) + self.S(28)          # 标题行 + 列名行
        self._log_view_h = self.S(360)            # 数据区视口(约 15 行)
        lh = HEAD_H + self._log_view_h + self.S(6)
        self._log_lh = lh
        # 位置: 主窗左侧; 屏幕放不下则靠左贴边
        x = max(self.S(12), self.root.winfo_x() - lw - self.S(12))
        w.geometry("%dx%d+%d+%d" % (lw, lh, x, self.root.winfo_y()))
        w.configure(bg=self.T["bg2"])
        # 头部(标题+列名, 固定不滚动)
        hdr = tk.Canvas(w, height=HEAD_H, highlightthickness=0, bd=0,
                        bg=self.T["bg2"])
        hdr.pack(side="top", fill="x")
        hdr.bind("<Button-1>", self._lw_click)
        hdr.bind("<B1-Motion>", self._lw_motion)
        hdr.bind("<ButtonRelease-1>",
                 lambda _e: setattr(self, "_lw_drag", None))
        self._log_hdr = hdr
        # 数据区(可滚动) + 细滚动条
        body = tk.Frame(w, bg=self.T["bg2"], highlightthickness=0, bd=0)
        body.pack(fill="both", expand=True)
        body.bind("<MouseWheel>", self._on_log_wheel)
        sb = tk.Canvas(body, width=self._sb_wpx, highlightthickness=0, bd=0,
                       bg=self.T["bg2"])
        sb.pack(side="right", fill="y")
        cv = tk.Canvas(body, highlightthickness=0, bd=0, bg=self.T["bg2"])
        cv.pack(side="left", fill="both", expand=True)
        cv.configure(yscrollincrement=1)          # 像素级丝滑滚动
        cv.bind("<Button-1>", self._lw_click)
        cv.bind("<B1-Motion>", self._lw_motion)
        cv.bind("<ButtonRelease-1>",
                lambda _e: setattr(self, "_lw_drag", None))
        cv.bind("<MouseWheel>", self._on_log_wheel)
        sb.bind("<MouseWheel>", self._on_log_wheel)
        sb.bind("<Button-1>", self._on_log_sb_press)
        sb.bind("<B1-Motion>", self._on_log_sb_drag)
        sb.bind("<ButtonRelease-1>",
                lambda _e: setattr(self, "_log_sb_drag", False))
        self._log_win = w
        self._log_cv = cv
        self._log_sb = sb
        self._draw_log_window()

    def _on_log_wheel(self, e):
        """日志数据区滚轮: 像素级滚动 + 同步细滚动条。"""
        if self._log_content_h > self._log_view_h + 2:
            self._wheel_scroll(self._log_cv, "log", e.delta, 50)
            self._sync_log_sb()
        return "break"

    def _sync_log_sb(self):
        """按日志区滚动位置重画细滚动条。"""
        sb = getattr(self, "_log_sb", None)
        if sb is None or not sb.winfo_exists():
            return
        sb.delete("all")
        vh = max(self._log_view_h, 1)
        ch = max(self._log_content_h, 1)
        if ch <= vh + 2:
            return
        w = self._sb_wpx
        track_h = vh
        frac = min(1.0, float(vh) / float(ch))
        th = max(self.S(18), int(track_h * frac))
        first, _last = self._log_cv.yview()
        ty = max(0, min(track_h - th, int(first * track_h)))
        self._log_thumb_y = ty
        self._log_thumb_h = th
        sb.create_rectangle(0, 0, w, track_h, fill=self.T["track"], outline="")
        sb.create_rectangle(1, ty + 1, w - 1, ty + th - 1,
                            fill=self.T["border2"], outline="")

    def _on_log_sb_press(self, e):
        vh = max(self._log_view_h, 1)
        if self._log_content_h <= vh + 2:
            return
        if self._log_thumb_y <= e.y <= self._log_thumb_y + self._log_thumb_h:
            self._log_sb_drag = True
            self._log_sb_dy = e.y - self._log_thumb_y
        else:
            self._log_cv.yview_moveto(max(0.0, min(1.0, e.y / vh)))
            self._sync_log_sb()

    def _on_log_sb_drag(self, e):
        if not getattr(self, "_log_sb_drag", False):
            return
        vh = max(self._log_view_h, 1)
        ch = max(self._log_content_h, 1)
        th = max(self.S(18), int(vh * vh / ch))
        ty = max(0, min(vh - th, e.y - self._log_sb_dy))
        self._log_cv.yview_moveto(ty / vh)
        self._sync_log_sb()

    def _lw_click(self, e):
        self._lw_drag = None
        try:
            tag = e.widget.gettags("current")   # 表头/数据区两个画布共用
        except Exception:
            tag = ()
        if "lw:close" in tag:
            self._close_log_window()
            return
        self._lw_drag = (e.x_root, e.y_root)

    def _lw_motion(self, e):
        d = getattr(self, "_lw_drag", None)
        w = getattr(self, "_log_win", None)
        if d and w is not None and w.winfo_exists():
            w.geometry("+%d+%d" % (w.winfo_x() + e.x_root - d[0],
                                   w.winfo_y() + e.y_root - d[1]))
            self._lw_drag = (e.x_root, e.y_root)

    def _close_log_window(self):
        w = getattr(self, "_log_win", None)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        self._log_win = None
        self._log_cv = None
        self._log_hdr = None
        self._log_sb = None

    def _log_cols(self, lw):
        """日志表列定义: (名称, x坐标, 对齐)。列宽适配 600 逻辑宽。"""
        return [
            ("时间", self.S(12), "w"),
            ("模型", self.S(75), "w"),
            ("状态", self.S(242), "center"),
            ("耗时", self.S(298), "e"),
            ("输入", self.S(356), "e"),
            ("输出", self.S(414), "e"),
            ("命中率", self.S(486), "e"),
            ("命中/未命中", self.S(588), "e"),
        ]

    def _draw_log_window(self):
        """绘制日志窗(标题/列名固定, 数据区可滚动); 每次数据刷新/主题切换后调用。"""
        w = getattr(self, "_log_win", None)
        if w is None or not w.winfo_exists():
            return
        T = self.T
        lw = getattr(self, "_log_lw", self.S(self._LOGW_W))
        cols = self._log_cols(lw)
        # ---------- 固定头部: 标题 + 列名 ----------
        hdr = getattr(self, "_log_hdr", None)
        if hdr is not None and hdr.winfo_exists():
            hdr.delete("all")
            HDR = self.S(30)
            hdr.create_rectangle(0, 0, lw, HDR, fill=T["bg2"], outline="")
            hdr.create_line(0, HDR, lw, HDR, fill=T["border"])
            hdr.create_text(self.S(12), HDR // 2, anchor="w", text="请求日志",
                            fill=T["text"], font=self._font(10, True))
            hdr.create_text(lw - self.S(14), HDR // 2, text="✕", fill=T["red"],
                            font=self._font(10, True), tags=("lw:close",))
            hy = HDR + self.S(15)
            for name, cx, anchor in cols:
                hdr.create_text(cx, hy, anchor=anchor, text=name,
                                fill=T["text4"], font=self._font(8, True))
            hdr.create_line(self.S(10), hy + self.S(10), lw - self.S(10),
                            hy + self.S(10), fill=T["border"])
        # ---------- 滚动数据区 ----------
        cv = self._log_cv
        cv.delete("all")
        recent = (self.last_data or {}).get("recent") or []
        row_h = self.S(24)
        if not recent:
            cv.create_text(self.S(12), self.S(14), anchor="w", text="暂无记录",
                           fill=T["text4"], font=self._font(8))
            self._log_content_h = row_h
            cv.configure(scrollregion=(0, 0, lw, self._log_content_h))
            self._sync_log_sb()
            return
        y = self.S(12)
        for r in recent[:50]:
            st = r.get("status") or "?"
            st_col = (T["green"] if st == "completed"
                      else (T["red"] if st == "error" else T["text4"]))
            mark = "✓" if st == "completed" else ("✗" if st == "error" else "—")
            try:
                tstr = datetime.datetime.fromtimestamp(
                    (r.get("ts") or 0) / 1000.0).strftime("%H:%M:%S")
            except Exception:
                tstr = "--:--:--"
            dur = (r.get("dur_ms") or 0) / 1000.0
            c_read = r.get("cread") or 0
            c_in = r.get("in_tok") or 0
            c_miss = max(c_in - c_read, 0)
            c_ratio = (c_read / c_in) if c_in else None
            if c_ratio is None:
                ratio_txt = "—"
                ratio_col = T["text4"]
            else:
                ratio_txt = "%.1f%%" % (min(c_ratio, 1.0) * 100)
                ratio_col = (T["green"] if c_ratio >= 0.9
                             else (T["amber"] if c_ratio >= 0.7 else T["red"]))
            vals = {"时间": tstr,
                    "模型": (r.get("model") or "?")[:17],
                    "状态": mark,
                    "耗时": "%.1fs" % dur,
                    "输入": "↓%s" % fmt_tok(r.get("in_tok")),
                    "输出": "↑%s" % fmt_tok(r.get("out_tok")),
                    "命中率": ratio_txt,
                    "命中/未命中": "%s / %s" % (fmt_tok(c_read), fmt_tok(c_miss))}
            for name, cx, anchor in cols:
                if name == "状态":
                    col = st_col
                elif name == "模型":
                    col = T["accent"]
                elif name == "时间":
                    col = T["text2"]
                elif name == "输入":
                    col = T["cyan"]
                elif name == "输出":
                    col = T["green"]
                elif name == "命中率":
                    col = ratio_col
                elif name == "命中/未命中":
                    col = T["purple"]
                else:
                    col = T["text3"]
                cv.create_text(cx, y, anchor=anchor, text=vals[name],
                               fill=col, font=self._font(8))
            y += row_h
        self._log_content_h = y + self.S(4)
        cv.configure(scrollregion=(0, 0, lw, self._log_content_h))
        self._sync_log_sb()

    def _on_click(self, e):
        self._drag = None
        self._drag_moved = False
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
                    self._start_fast_poll()
                return
        # 顶栏空白处(整个顶栏画布): 开始拖拽
        self._drag = (e.x_root, e.y_root)

    def _on_drag(self, e):
        d = getattr(self, "_drag", None)
        if d:
            # 位移超过阈值才算真拖动(点按顶栏空白不应写注册表)
            if abs(e.x_root - d[0]) > 3 or abs(e.y_root - d[1]) > 3:
                self._drag_moved = True
                x = self.root.winfo_x() + e.x_root - d[0]
                y = self.root.winfo_y() + e.y_root - d[1]
                self.root.geometry("+%d+%d" % (x, y))
                self._drag = (e.x_root, e.y_root)

    def _on_drag_end(self, _e=None):
        """拖动结束: 仅在真实移动过时记录窗口位置, 重启回到上次位置。"""
        moved = getattr(self, "_drag_moved", False)
        self._drag = None
        self._drag_moved = False
        if moved:
            self._save_pos()

    def _save_pos(self):
        try:
            zu.save_override({"win_x": int(self.root.winfo_x()),
                              "win_y": int(self.root.winfo_y())})
        except Exception:
            pass

    # ---------- 底栏拖动调节高度 ----------
    def _on_resize_start(self, e):
        self._resize_drag = (e.y_root, self.h)
        self._resize_moved = False
        self._resize_last_h = self.h

    def _on_resize_motion(self, e):
        d = getattr(self, "_resize_drag", None)
        if not d:
            return
        dy = e.y_root - d[0]
        if not self._resize_moved and abs(dy) <= 3:
            return                      # 微小抖动: 不算拖动
        self._resize_moved = True
        new_h = d[1] + dy
        # 上限 = min(工作区剩余, 内容完整所需高) —— 内容比它矮时不要空余白
        full_need = getattr(self, "_full_need_h", 0) or 10 ** 9
        max_h = max(self.S(300), self._sh - self.root.winfo_y() - self.S(4))
        new_h = max(self.S(220), min(int(new_h), max_h, max(full_need, self.S(220))))
        if abs(new_h - self.h) <= 2:
            return
        self.h = new_h
        self._max_h = new_h             # 拖动值即新的高度上限
        self._custom_h = max(0, int(round(new_h / self.scale)))
        # 窗口实时跟随鼠标(必须显式调用: _draw 定稿时 want==h 不会重复设尺寸)
        try:
            self.root.geometry("%dx%d" % (self.w, self.h))
        except Exception:
            pass
        # 内容联动重绘(节流): 模型卡视口/主布局随高度重排
        if abs(new_h - getattr(self, "_resize_last_h", 0)) > self.S(6):
            self._resize_last_h = new_h
            if self.last_data:
                self._draw(self.last_data)

    def _on_resize_end(self, _e=None):
        moved = getattr(self, "_resize_moved", False)
        self._resize_drag = None
        self._resize_moved = False
        if not moved:
            return
        # 记录为固定高度(逻辑px), 重启保留
        self._custom_h = max(0, int(round(self.h / self.scale)))
        self.cfg["win_h"] = self._custom_h
        try:
            zu.save_override({"win_h": self._custom_h})
        except Exception:
            pass
        # 定稿重绘: 节流期间的小幅移动可能未重排, 这里补齐
        if self.last_data:
            self._draw(self.last_data)

    def _on_resize_reset(self, _e=None):
        """双击底栏: 恢复自动高度(0)。"""
        self._resize_drag = None
        self._resize_moved = False
        self._custom_h = 0
        self.cfg["win_h"] = 0
        self._max_h = self._sh - self.S(8)
        try:
            zu.save_override({"win_h": 0})
        except Exception:
            pass
        if self.last_data:
            self._draw(self.last_data)

    # ---------- 最小化到任务栏 ----------
    def _is_iconic(self):
        """窗口当前是否处于最小化态(Win32 IsIconic, 权威判定)。
        注意: 必须用外层框架句柄 wm_frame() —— winfo_id() 是 tk 内部子窗口,
        Windows 最小化的是外层框架(且 overrideredirect 切换会换框架句柄,
        故每次调用都要重新获取)。"""
        try:
            h = self.root.wm_frame()
            hwnd = h if isinstance(h, int) else int(h, 16)
            return bool(_ctypes.windll.user32.IsIconic(hwnd))
        except Exception:
            return False

    def _minimize(self):
        """无边框窗口没有任务栏按钮: 先临时恢复系统装饰获得任务栏图标,
        再最小化; 从任务栏还原时恢复无边框悬浮形态。
        最小化期间暂停轮询(省资源), 还原时立即刷新一轮。
        注意: overrideredirect 切换会触发伪 Map 事件 —— 用确认门过滤,
        只有确认已进入最小化态后, 后续 Map 事件才视为"还原"。"""
        self._saved_geom = self.root.geometry()
        self._minimized = True
        self._minimize_confirmed = False
        # 暂停轮询: 取消已排定的刷新/快速轮询定时器
        for attr in ("_timer_id", "_fast_id"):
            tid = getattr(self, attr, None)
            if tid:
                try:
                    self.root.after_cancel(tid)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._hide_tip()
        self.root.overrideredirect(False)
        self.root.iconify()
        self.root.after(450, self._confirm_minimize)

    def _confirm_minimize(self):
        """延迟确认: 450ms 后若已进入最小化态则开门;
        若窗口已不在最小化(极少数竞态), 直接执行恢复收尾。"""
        if not getattr(self, "_minimized", False):
            return
        if self._is_iconic():
            self._minimize_confirmed = True
        else:
            self._do_restore()

    def _on_map(self, _e=None):
        if not getattr(self, "_minimized", False):
            return
        if not getattr(self, "_minimize_confirmed", False):
            return                          # 过渡期伪 Map 事件, 忽略
        if self._is_iconic():
            return                          # 仍在最小化态, 不是还原
        self._do_restore()

    def _do_restore(self):
        """从任务栏还原: 恢复无边框悬浮形态 + 立即刷新一轮。"""
        self._minimized = False
        self._minimize_confirmed = False
        self.root.overrideredirect(True)
        try:
            self.root.geometry(self._saved_geom)
        except Exception:
            pass
        self.root.attributes("-topmost", True)
        # 还原: 立即刷新一轮并恢复定时器; 1.2s 后补一次上屏
        # (后台数据约 1s 就绪, 否则要等下一个刷新周期才显示)
        self.refreshing = False
        self._kick_refresh()
        self._poll_timer()
        self.root.after(1200, self._quick_redraw)

    def _quick_redraw(self):
        """短延迟补刷新: 用于还原后, 让刚就绪的后台数据尽快上屏。"""
        if getattr(self, "_minimized", False):
            return
        self._poll()
        if self.last_data:
            try:
                self._draw(self.last_data)
                self._check_warn(self.last_data.get("quota") or {})
            except Exception:
                pass

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
        lab("窗口高度(px, 0=自动)")
        hh = tk.IntVar(value=int(self.cfg.get("win_h") or 0))
        tk.Spinbox(win, from_=0, to=2000, increment=20, textvariable=hh,
                   width=6, font=f(10), bg=self.T["card"], fg=self.T["text"],
                   buttonbackground=self.T["card2"],
                   insertbackground=self.T["text"]).grid(row=row - 1, column=1,
                                                         sticky="w")
        tk.Label(win, text="也可拖动面板底栏调节", bg=self.T["bg2"],
                 fg=self.T["text4"], font=f(8)).grid(row=row - 1, column=2,
                                                     sticky="w", padx=(10, 0))
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
                row=row + ci // 3, column=ci % 3, sticky="w",
                padx=(0, 4), pady=(2, 2))
        row += (len(MODS) - 1) // 3 + 1      # 复选框从标签下一行起排, 保存按钮再下移

        def save():
            mods = [k for k, _ in [("kpi", 1), ("quota", 1), ("dist", 1),
                                   ("trend", 1), ("models", 1)] if mod_vars[k].get()]
            if not mods:
                mods = ["kpi", "quota", "dist", "trend", "models"]
            wh = max(0, min(int(hh.get() or 0), 2000))
            try:
                zu.save_override({"refresh_seconds": int(ref.get()),
                                  "warn_pct": int(wr.get()), "crit_pct": int(cr.get()),
                                  "theme": th.get(), "modules": ",".join(mods),
                                  "win_h": wh})
            except Exception:
                pass
            self.cfg.update({"refresh_seconds": int(ref.get()), "warn_pct": int(wr.get()),
                             "crit_pct": int(cr.get()), "theme": th.get(),
                             "modules": mods, "win_h": wh})
            # 自定义高度立即生效: 更新上限, 重绘时按新上限自适应
            self._custom_h = wh
            self._max_h = min(self.S(wh) if wh > 0 else (self._sh - self.S(8)),
                              self._sh - self.S(8))
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

    def _seg_text(self, cv, x, y, segs, font=None):
        """多色文字片段依次绘制(anchor=w), 返回结束 x —— 用于箭头/数值分色。"""
        f = font or self._font(8)
        for txt, col in segs:
            cv.create_text(x, y, anchor="w", text=txt, fill=col, font=f)
            x += f.measure(txt)
        return x

    def _seg_text_r(self, cv, x_right, y, segs, font=None):
        """多色文字片段(右对齐): 从 x_right 减去总宽起画。"""
        f = font or self._font(8)
        total = sum(f.measure(txt) for txt, _ in segs)
        return self._seg_text(cv, x_right - total, y, segs, font=f)

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
        self._models_win_id = None       # 模型卡未绘制, 滚动条同步禁用
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
        # 额度预警红点(标题右侧): 危急=红 / 警告=琥珀; 正常不显示
        wlv = max(self._warn_lv.get("block", 0), self._warn_lv.get("week", 0))
        if wlv > 0:
            f11 = self._font(11, True)
            tx = self.S(22) + f11.measure("ZCode 监控") + self.S(7)
            dr = self.S(4)
            col = T["red"] if wlv >= 2 else T["amber"]
            c.create_oval(tx, cy - dr, tx + 2 * dr, cy + dr,
                          fill=col, outline=col)
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
        self._models_win_id = None       # 嵌入窗口已随 delete 移除, 重绘时重建
        self._mname_map = {}             # 模型名文字项 -> 全名(悬浮提示)
        self._hide_tip()
        # 每帧有效高度上限(默认=用户上限; 自定义过小时由模型卡段自动补足)
        self._max_h_eff = self._max_h
        self._full_need_h = 0            # 全部展开所需高(模型卡段重新计算)
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

        # --- 模型速度(卡内独立滚动区; 请求日志为独立悬浮窗) ---
        if models and "models" in mods:
            x1 = self.S(self.PAD)
            x2 = self.w - self.S(self.PAD)
            cw = x2 - x1
            row_h = self.S(152)
            list_h = self.S(8) + row_h * len(models)
            mc_content_h = list_h
            # 视口上限 = 剩余屏幕空间: 让本卡吸收全部余量 ——
            # 面板本身永不滚动, 只在模型卡内部滚动。
            # 扣 95 = 顶栏40 + 卡头30 + 卡后间距7 + 底栏18 (逻辑值)
            # 自定义上限过小时自动补足到"固定模块+最小视口"的可行高度
            room = (self._max_h_eff - y - self.S(95))
            if room < self.S(40):
                self._max_h_eff = min(y + self.S(95) + self.S(40),
                                      self._sh - self.S(8))
                room = self._max_h_eff - y - self.S(95)
            view_h = min(mc_content_h, max(self.S(40), room))
            card_h = self.S(30) + view_h
            # 记录"全部展开所需窗口高"(拖动调高的上限: 不产生底部空白)
            self._full_need_h = (self.S(40) + y + self.S(30)
                                 + mc_content_h + self.S(self.GAP) + self.S(18))
            self._card(y, card_h)
            ix = x1 + self.S(10)
            c.create_text(ix, y + self.S(14), anchor="w", text="模型速度",
                          fill=T["text3"], font=self._font(9, True))
            c.create_text(x2 - self.S(10), y + self.S(14), anchor="e",
                          text="请求日志 ↗",
                          fill=T["accent"], font=self._font(8, True),
                          tags=("log:open",))
            # 内嵌滚动区(卡内滚动不影响主内容区)
            self._models_view_h = view_h
            self._models_content_h = mc_content_h
            self._models_cv_w = cw - self._sb_wpx
            self.mid_cv.create_window(x1, y + self.S(29), anchor="nw",
                                      window=self.models_wrap,
                                      width=cw, height=view_h)
            mc = self.models_cv
            mc.delete("all")
            mcw = self._models_cv_w
            mc.configure(scrollregion=(0, 0, mcw, mc_content_h))
            lx = self.S(10)
            rx = mcw - self.S(8)
            my = self.S(6)
            for name, m in models:
                tps = m.get("weighted_tps")
                col = T["green"] if (tps or 0) >= 80 else (T["amber"] if (tps or 0) >= 30 else T["red"])
                _nid = mc.create_text(lx, my + self.S(12), anchor="w",
                                      text=name if len(name) <= 18 else name[:17] + "…",
                                      fill=T["accent"], font=self._font(9, True))
                self._mname_map[_nid] = name     # 悬浮提示: 截断名 -> 全名
                mc.create_text(rx - self.S(135), my + self.S(12),
                               anchor="e", text="%s tok/s" % fmt_i_round1(tps), fill=col,
                               font=self._font(9, True))
                # 缓存命中率(百分比): tok/s 右侧
                cr = m.get("cache_ratio")
                if cr is not None:
                    rc = (T["green"] if cr >= 0.9
                          else (T["amber"] if cr >= 0.7 else T["red"]))
                    mc.create_text(rx - self.S(72), my + self.S(12), anchor="e",
                                   text="⚡%.1f%%" % (min(cr, 1.0) * 100),
                                   fill=rc, font=self._font(8, True))
                mc.create_text(rx, my + self.S(12),
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
                        (f1(du.get("avg")), f1(du.get("median")), f1(du.get("p90"))))]
                sy = my + self.S(36)
                for lab, val in sub:
                    mc.create_text(lx, sy, anchor="w", text=lab, fill=T["text4"],
                                   font=self._font(8))
                    mc.create_text(lx + self.S(58), sy, anchor="w", text=val,
                                   fill=T["text3"], font=self._font(8))
                    sy += self.S(21)
                # 输入/输出: 左右分栏; 箭头分色(↓输入=青, ↑输出=绿, 对齐参考图)
                f8 = self._font(8)
                mc.create_text(lx, sy, anchor="w", text="输入", fill=T["text4"],
                               font=f8)
                self._seg_text(mc, lx + self.S(58), sy, [
                    ("↓", T["cyan"]),
                    ("均 %s · 大 %s" % (fmt_tok(it.get("avg")),
                                       fmt_tok(it.get("max"))),
                     T["text3"])], font=f8)
                xm = lx + (rx - lx) // 2
                mc.create_text(xm, sy, anchor="w", text="输出", fill=T["text4"],
                               font=f8)
                self._seg_text(mc, xm + self.S(58), sy, [
                    ("↑", T["green"]),
                    ("均 %s · 大 %s" % (fmt_tok(ot.get("avg")),
                                       fmt_tok(ot.get("max"))),
                     T["text3"])], font=f8)
                sy += self.S(21)
                # 缓存: 命中均/未命中均(方案 A1; 命中率已放表头 tok/s 右侧)
                creq = m.get("requests") or 0
                c_read = m.get("cache_read_tokens") or 0
                c_in = m.get("cache_in_tokens") or 0
                hit_avg = (c_read / creq) if creq else 0
                miss_avg = (max(c_in - c_read, 0) / creq) if creq else 0
                mc.create_text(lx, sy, anchor="w", text="缓存", fill=T["text4"],
                               font=f8)
                self._seg_text(mc, lx + self.S(58), sy, [
                    ("⚡", T["purple"]),
                    ("命中均 %s · 未命中均 %s" % (fmt_tok(hit_avg),
                                                  fmt_tok(miss_avg)),
                     T["text3"])], font=f8)
                sy += self.S(21)
                mc.create_line(lx, my + self.S(139), rx, my + self.S(139),
                               fill=T["border"])
                my += row_h
            self._models_win_id = True
            self._sync_models_sb()
            y += card_h + self.S(self.GAP)

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
        # 目标: 面板完整展开、主区不滚动; 超出部分由模型卡内部滚动吸收
        # (自定义高度过小时 _max_h_eff 已在模型卡段自动补足)
        content_h = y
        self.mid_cv.configure(scrollregion=(0, 0, self.w, content_h))
        want = min(self._max_h_eff,
                   self.S(40) + content_h + self.S(18))
        # 窗口底边防出屏: 若下缘超出屏幕则上移(窗口可近全屏高)
        wy = self.root.winfo_y()
        if wy + want > self._sh - self.S(4):
            wy = max(0, self._sh - self.S(4) - want)
        if abs(want - self.h) > self.S(6) or wy != self.root.winfo_y():
            self.h = want
            try:
                self.root.geometry("%dx%d+%d+%d" % (self.w, self.h,
                                                    self.root.winfo_x(), wy))
            except Exception:
                pass
        # 请求日志悬浮窗(若已打开)随数据刷新同步内容
        self._draw_log_window()


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
