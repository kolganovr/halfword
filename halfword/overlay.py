"""Плашка с подсказкой у каретки: поверх всех окон, не забирает фокус, клики проходят насквозь."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import time
import tkinter as tk
import tkinter.font as tkfont

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x8, 0x80, 0x80000
WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x20, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

# DWM: скругление углов (Win11) и тень. На Win10 атрибуты молча не применяются.
DWMWA_NCRENDERING_POLICY, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWA_BORDER_COLOR = 2, 33, 34
DWMNCRP_ENABLED, DWMWCP_ROUNDSMALL = 2, 3
try:
    _dwm = ctypes.WinDLL("dwmapi")
    _dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
except OSError:  # pragma: no cover
    _dwm = None


class _MARGINS(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


# Палитры. Светлая — исходная; тёмная — под тёмную тему Windows.
LIGHT = dict(bg="#F4F4F4", fg="#6B6B6B", fg_long="#8E8E8E", hint="#A0A0A0", border="#C8C8C8",
             key_border="#D2D2D2", accent="#7A6FD0")
DARK = dict(bg="#2B2B2B", fg="#C8C8C8", fg_long="#9A9A9A", hint="#7A7A7A", border="#3C3C3C",
            key_border="#4A4A4A", accent="#A99CFF")
PALETTES = {"light": LIGHT, "dark": DARK}
BG, FG, HINT, BORDER, FG_LONG = LIGHT["bg"], LIGHT["fg"], LIGHT["hint"], LIGHT["border"], LIGHT["fg_long"]

ALPHA = 0.93
FADE_STEPS, FADE_STEP_MS = 4, 20     # ~80 мс при появлении из скрытого состояния
THEME_RECHECK_S = 30                 # реестр перечитываем не чаще
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def read_apps_use_light() -> int | None:
    """AppsUseLightTheme из реестра: 0 — тёмная тема приложений, 1 — светлая, None — не удалось."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as k:
            return int(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except Exception:
        return None


def resolve_theme(theme: str, reader=None) -> str:
    """auto|light|dark -> light|dark. Неизвестное значение и сбой реестра = светлая."""
    if theme in PALETTES:
        return theme
    return "dark" if (reader or read_apps_use_light)() == 0 else "light"


def palette_for(theme: str, reader=None) -> dict:
    return PALETTES[resolve_theme(theme, reader)]


_HINT_RE = re.compile(r"^(\S+)(?:\s+(✦{1,2}|⇄|✎))?(?:\s+(\d+/\d+))?$")


def parse_hint(hint: str) -> tuple[str, str, str]:
    """«Tab ✦✦ 2/3» -> (клавиша, значок источника, счётчик); отсутствующее — пустая строка."""
    m = _HINT_RE.match((hint or "").strip())
    if not m:
        return (hint or "").strip(), "", ""
    return m.group(1), m.group(2) or "", m.group(3) or ""


def needs_update(prev: tuple | None, new: tuple, visible: bool) -> bool:
    """Перерисовывать ли плашку: скрыта, первый показ или изменилось (text, hint, long, px, x, y, theme)."""
    return not visible or prev != new


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]


def work_area(x: int, y: int) -> tuple[int, int, int, int]:
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hm = user32.MonitorFromPoint(wt.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    if hm and user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, 1 << 15, 1 << 15


def _colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


class Overlay:
    def __init__(self, root: tk.Tk, family: str = "Segoe UI", theme: str = "auto"):
        self.theme = theme
        self._theme_name: str | None = None   # применённая палитра (light|dark)
        self._theme_t = 0.0                   # когда последний раз читали реестр
        self._applied_theme: str | None = None  # палитра, которой раскрашены виджеты
        self._applied: dict[str, dict] = {}   # последние применённые опции виджетов
        self._state: tuple | None = None      # последние (text, hint, long, px, x, y, theme)
        self._size: tuple[int, int] | None = None
        self._pos: tuple[int, int] | None = None
        self._px = 0
        self._one = 0
        self._shown_parts: dict[str, bool] = {"src": False, "cnt": False}
        self._fade_job = None
        pal = self._pick_palette(force=True)
        self._applied_theme = self._theme_name
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", ALPHA)
        self.win.configure(bg=pal["border"])
        self.inner = tk.Frame(self.win, bg=pal["bg"])
        self.inner.pack(padx=1, pady=1)
        self.font = tkfont.Font(family=family, size=-16)
        self.hint_font = tkfont.Font(family=family, size=-11)
        self.text = tk.Label(self.inner, bg=pal["bg"], fg=pal["fg"], font=self.font, padx=4, pady=0)
        self.text.pack(side="left")
        # подсказка: клавиша в рамке + значок источника + счётчик вариантов
        self.hint_box = tk.Frame(self.inner, bg=pal["bg"])
        self.hint_box.pack(side="left", padx=(0, 3))
        self.key_box = tk.Frame(self.hint_box, bg=pal["bg"], highlightthickness=1,
                                highlightbackground=pal["key_border"], highlightcolor=pal["key_border"])
        self.key_box.pack(side="left")
        self.key = tk.Label(self.key_box, text="Tab", bg=pal["bg"], fg=pal["hint"], font=self.hint_font,
                            padx=3, pady=0)
        self.key.pack()
        self.src = tk.Label(self.hint_box, bg=pal["bg"], fg=pal["accent"], font=self.hint_font, padx=0)
        self.cnt = tk.Label(self.hint_box, bg=pal["bg"], fg=pal["hint"], font=self.hint_font, padx=0)
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                              | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST)
        self._dwm_style(pal)
        user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False

    # --- DWM: скругление и тень; любой отказ молча игнорируем ---
    def _dwm_style(self, pal: dict):
        if not _dwm:
            return
        try:
            v = ctypes.c_int(DWMWCP_ROUNDSMALL)
            _dwm.DwmSetWindowAttribute(self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(v), 4)
        except Exception:
            pass
        try:
            self._dwm_border(pal)
        except Exception:
            pass
        try:  # тень: включённый NC-рендеринг + рамка в 1 px внутрь клиентской области
            v = ctypes.c_int(DWMNCRP_ENABLED)
            _dwm.DwmSetWindowAttribute(self.hwnd, DWMWA_NCRENDERING_POLICY, ctypes.byref(v), 4)
            m = _MARGINS(1, 1, 1, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(wt.HWND(self.hwnd), ctypes.byref(m))
        except Exception:
            pass

    def _dwm_border(self, pal: dict):
        """Цвет рамки DWM (Win11) — в тон палитре, чтобы скруглённый контур не был белым."""
        c = wt.DWORD(_colorref(pal["border"]))
        _dwm.DwmSetWindowAttribute(self.hwnd, DWMWA_BORDER_COLOR, ctypes.byref(c), 4)

    # --- тема ---
    def set_theme(self, theme: str):
        self.theme = theme
        pal = self._pick_palette(force=True)
        if self._theme_name != self._applied_theme:
            self._apply_palette(pal)
        self._state = None  # следующий show перерисует текст в новых цветах

    def _pick_palette(self, force: bool = False) -> dict:
        """Палитра по теме; реестр для auto перечитываем не чаще THEME_RECHECK_S."""
        now = time.monotonic()
        if self.theme in PALETTES:
            self._theme_name = self.theme
        elif force or self._theme_name is None or now - self._theme_t >= THEME_RECHECK_S:
            self._theme_name = resolve_theme(self.theme)
            self._theme_t = now
        return PALETTES[self._theme_name]

    def _apply_palette(self, pal: dict):
        self._applied_theme = self._theme_name
        self._applied.clear()
        self.win.configure(bg=pal["border"])
        for w in (self.inner, self.hint_box, self.key_box, self.key, self.text, self.src, self.cnt):
            w.configure(bg=pal["bg"])
        self.key_box.configure(highlightbackground=pal["key_border"], highlightcolor=pal["key_border"])
        self.key.configure(fg=pal["hint"])
        self.src.configure(fg=pal["accent"])
        self.cnt.configure(fg=pal["hint"])
        try:
            if _dwm:
                self._dwm_border(pal)
        except Exception:
            pass

    # --- вывод ---
    def _cfg(self, name: str, widget, **opts) -> bool:
        """configure только при изменении опций; True — что-то поменялось (нужен пересчёт размера)."""
        if self._applied.get(name) == opts:
            return False
        widget.configure(**opts)
        self._applied[name] = dict(opts)
        return True

    def _part(self, name: str, widget, value: str) -> bool:
        """Показать/спрятать необязательную часть подсказки (значок, счётчик)."""
        dirty = self._cfg(name, widget, text=value)
        if bool(value) != self._shown_parts[name]:
            if value:
                widget.pack(side="left", padx=(3, 0))
            else:
                widget.pack_forget()
            self._shown_parts[name] = bool(value)
            dirty = True
        return dirty

    def show(self, text: str, x: int, y: int, line_h: int, hint: str = "Tab", long: bool = False):
        px = max(12, min(40, int(line_h * 0.72)))
        pal = self._pick_palette()
        state = (text, hint, long, px, x, y, self._theme_name)
        if not needs_update(self._state, state, self.visible):
            return
        was_visible = self.visible
        self._state = state
        if self._applied_theme != self._theme_name:
            self._apply_palette(pal)
        dirty = self._size is None
        if px != self._px:
            self.font.configure(size=-px)
            self.hint_font.configure(size=-max(9, int(px * 0.65)))
            self._px = px
            self._one = self.font.metrics("linespace") + 2
            dirty = True
        left, _top, right, bottom = work_area(x, y)
        # длинную подсказку переносим, чтобы не уезжала за край экрана
        wrap = max(px * 12, min(px * 40, right - x - px * 5)) if long else 0
        key, src, cnt = parse_hint(hint)
        dirty |= self._cfg("text", self.text, text=text, fg=pal["fg_long"] if long else pal["fg"], wraplength=wrap)
        dirty |= self._cfg("key", self.key, text=key)
        dirty |= self._part("src", self.src, src)
        dirty |= self._part("cnt", self.cnt, cnt)
        if dirty:
            self.win.update_idletasks()  # единственный пересчёт размера на show
            self._size = (self.win.winfo_reqwidth(), self.win.winfo_reqheight())
        w, h = self._size
        top = y + (line_h - self._one) // 2   # первая строка подсказки — на строке курсора
        if top + h > bottom:
            top = y - h - 2                    # внизу экрана — над строкой
        left_x = max(left, min(x + 2, right - w))
        if (left_x, top) != self._pos or not was_visible:
            self.win.geometry(f"+{left_x}+{top}")
            self._pos = (left_x, top)
            if not was_visible:
                self.win.update_idletasks()    # позиция должна примениться до показа
        if was_visible:
            return
        self._cancel_fade()
        self.win.attributes("-alpha", 0.0)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.visible = True
        self._fade(1)

    def _fade(self, k: int):
        self._fade_job = None
        try:
            self.win.attributes("-alpha", ALPHA * k / FADE_STEPS)
        except tk.TclError:
            return
        if k < FADE_STEPS:
            self._fade_job = self.win.after(FADE_STEP_MS, self._fade, k + 1)

    def _cancel_fade(self):
        if self._fade_job is not None:
            try:
                self.win.after_cancel(self._fade_job)
            except tk.TclError:
                pass
            self._fade_job = None

    def hide(self):
        if self.visible:
            self._cancel_fade()
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False
