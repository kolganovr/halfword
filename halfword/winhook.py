"""Глобальный хук клавиатуры/мыши (WH_KEYBOARD_LL / WH_MOUSE_LL) и вставка текста через SendInput.

Колбэк хука должен отвечать мгновенно, поэтому здесь только перевод клавиши в событие
и решение «проглотить Tab или нет»; вся логика — в потоке приложения через очередь.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass

from .layout import KeyMap

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
MAGIC = 0x5354_5950  # dwExtraInfo наших собственных нажатий: хук их пропускает
REPLAY = 0x5354_5951  # нажатия, отложенные на время вставки и повторённые после: хук их обрабатывает как обычные

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_QUIT = 0x12
MOUSE_DOWN = {0x201, 0x204, 0x207, 0x20B}  # L/R/M/X button down
MOUSE_WHEEL = {0x20A, 0x20E}

VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE, VK_RIGHT = 0x08, 0x09, 0x0D, 0x1B, 0x27
VK_UP, VK_DOWN = 0x26, 0x28
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_MASK = 0xE8  # неназначенная клавиша: гасит активацию меню, когда Alt отпустят после проглоченного Alt+↓
VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_F12, VK_PACKET = 0x10, 0x11, 0x12, 0x14, 0x7B, 0xE7
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_PAUSE, VK_SCROLL = 0x13, 0x91
HOTKEY_VK = {"pause": VK_PAUSE, "scroll_lock": VK_SCROLL}  # ручная клавиша раскладки (double_shift — два тапа Shift)
SHIFTS = (0x10, 0xA0, 0xA1)
DOUBLE_SHIFT_S = 0.4   # два тапа Shift подряд не дольше
GATE_DELIMS = set(".,;:!?)]}\"'»…")  # и пробельные: после них автозамена раскладки смотрит на закрытое слово
MODIFIERS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C, 0x14, 0x90, 0x91}
NAV = set(range(0x21, 0x29)) | {0x2D, 0x2E}  # PgUp..Down, Insert, Delete


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetKeyboardLayout.argtypes = [wt.DWORD]
user32.GetKeyboardLayout.restype = wt.HKL
user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.ToUnicodeEx.argtypes = [wt.UINT, wt.UINT, ctypes.c_char_p, wt.LPWSTR, ctypes.c_int, wt.UINT, wt.HKL]
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.LoadKeyboardLayoutW.argtypes = [wt.LPCWSTR, wt.UINT]
user32.LoadKeyboardLayoutW.restype = wt.HKL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(wt.HKL)]
user32.GetKeyboardLayoutList.restype = ctypes.c_int
user32.MapVirtualKeyExW.argtypes = [wt.UINT, wt.UINT, wt.HKL]
user32.MapVirtualKeyExW.restype = wt.UINT
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.OpenProcess.restype = wt.HANDLE
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


def _down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def process_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


APP_NAMES = {
    "telegram.exe": "Telegram", "obsidian.exe": "Obsidian", "winword.exe": "Word", "notepad.exe": "Блокнот",
    "outlook.exe": "Outlook", "olk.exe": "Outlook", "slack.exe": "Slack", "discord.exe": "Discord",
    "whatsapp.exe": "WhatsApp", "chrome.exe": "", "msedge.exe": "", "firefox.exe": "", "brave.exe": "",
    "opera.exe": "", "browser.exe": "",
}
TITLE_TAIL_RE = re.compile(r"\s[-—–|]\s[^-—–|]*(?:Chrome|Edge|Firefox|Opera|Brave|Яндекс|Блокнот|Notepad|Obsidian|"
                           r"Word|Telegram)[^-—–|]*$", re.I)


def clean_title(exe: str, title: str) -> str:
    """Шапка промпта: «Приложение — заголовок окна» без хвостов браузера, до 80 символов."""
    title = title.replace("\u200b", "").strip()
    for _ in range(2):
        title = TITLE_TAIL_RE.sub("", title).strip()
    app = APP_NAMES.get(exe, exe[:-4].capitalize() if exe.endswith(".exe") else exe)
    parts = [x for x in (app, title) if x]
    if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
        parts = parts[1:]
    return " — ".join(parts)[:80]


def window_header(hwnd, exe: str) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return clean_title(exe, buf.value)


def masked_alt_up(alt_vk: int):
    """Вместо отпускания Alt: пустая клавиша + Alt-up (помечены MAGIC — хук пропустит).

    Если Alt отпустить сразу после проглоченного Alt+↓, окно решит, что нажат «голый» Alt,
    и откроет меню (в новом Блокноте — подсказки клавиш). Так же делает AutoHotkey (MenuMaskKey).
    """
    arr = (INPUT * 3)()
    for j, (vk, flags) in enumerate(((VK_MASK, 0), (VK_MASK, 0x0002), (alt_vk, 0x0002))):
        arr[j].type = 1
        arr[j].ki = KEYBDINPUT(vk, 0, flags, 0, MAGIC)
    user32.SendInput(3, arr, ctypes.sizeof(INPUT))


def send_key(vk: int, scan: int = 0, extended: bool = False, extra: int = REPLAY):
    """Нажать и отпустить клавишу (по умолчанию как повтор: хук обработает её как набранную руками)."""
    arr = (INPUT * 2)()
    for j, flags in enumerate((0, 0x0002)):  # KEYUP
        arr[j].type = 1
        arr[j].ki = KEYBDINPUT(vk, scan, flags | (0x0001 if extended else 0), 0, extra)  # EXTENDEDKEY
    user32.SendInput(2, arr, ctypes.sizeof(INPUT))


def send_key_shifted(vk: int, scan: int, extended: bool, shift: bool):
    """Повторить нажатие клавишей (с Shift, если он был зажат), чтобы хук и окно перевели её в текущей раскладке."""
    if not shift:
        send_key(vk, scan, extended)
        return
    arr = (INPUT * 4)()
    for j, (k, sc, flags) in enumerate(((VK_SHIFT, 0, 0), (vk, scan, 0), (vk, scan, 0x0002), (VK_SHIFT, 0, 0x0002))):
        arr[j].type = 1
        arr[j].ki = KEYBDINPUT(k, sc, flags | (0x0001 if extended and k == vk else 0), 0, REPLAY)
    user32.SendInput(4, arr, ctypes.sizeof(INPUT))


# --- раскладки: какие установлены, какая у окна с фокусом, переключение ---
LANG_ENGLISH, LANG_RUSSIAN = 0x09, 0x19   # основной язык в младшем слове HKL
_OEM_VKS = (0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF)  # VK_OEM_1…8, PLUS, COMMA, MINUS, PERIOD
_KEY_VKS = tuple(range(0x41, 0x5B)) + _OEM_VKS + tuple(range(0x30, 0x3A))


def primary_lang(hkl) -> int:
    return (int(hkl or 0) & 0xFFFF) & 0x3FF


def focus_target(fg=None):
    """Окно с фокусом (hwndFocus потока переднего окна); нет — переданное окно верхнего уровня."""
    gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(0, ctypes.byref(gti)) and gti.hwndFocus:
        return gti.hwndFocus
    return fg or user32.GetForegroundWindow()


def focus_hkl(fg=None) -> int:
    """Раскладка потока с фокусом: у окна верхнего уровня она бывает другой или ещё не переключённой."""
    tid = user32.GetWindowThreadProcessId(focus_target(fg), None)
    return int(user32.GetKeyboardLayout(tid) or 0)


def installed_layouts() -> list[int]:
    n = user32.GetKeyboardLayoutList(0, None)
    if n <= 0:
        return []
    arr = (wt.HKL * n)()
    n = user32.GetKeyboardLayoutList(n, arr)
    return [int(h or 0) for h in arr[:max(n, 0)]]


def _vk_char(vk: int, shift: bool, hkl: int) -> str:
    """Символ клавиши в раскладке; пусто — мёртвая клавиша, лигатура или не печатается."""
    state = (ctypes.c_ubyte * 256)()
    if shift:
        state[VK_SHIFT] = 0x80
    buf = ctypes.create_unicode_buffer(8)
    scan = user32.MapVirtualKeyExW(vk, 0, hkl)  # MAPVK_VK_TO_VSC
    n = user32.ToUnicodeEx(vk, scan, ctypes.cast(state, ctypes.c_char_p), buf, 8, 0x4, hkl)
    ch = buf.value[:1] if n == 1 else ""
    return ch if ch and ch.isprintable() and not ch.isspace() else ""


def build_keymap(en_hkl: int, ru_hkl: int) -> KeyMap | None:
    """Пары символов одной клавиши в английской и русской раскладках (ToUnicodeEx, без Shift и с Shift).

    Берутся пары, где с обеих сторон один печатаемый символ, а символы не повторяются; None — таблица неполная.
    """
    en, ru, seen_e, seen_r = [], [], set(), set()
    for vk in _KEY_VKS:
        for shift in (False, True):
            e, r = _vk_char(vk, shift, en_hkl), _vk_char(vk, shift, ru_hkl)
            if not e or not r or e == r or e in seen_e or r in seen_r:
                continue
            en.append(e)
            ru.append(r)
            seen_e.add(e)
            seen_r.add(r)
    if sum(1 for c in en if c.isascii() and c.isalpha()) < 52:  # a–z обоих регистров
        return None
    return KeyMap("".join(en), "".join(ru))


@dataclass
class LayoutPair:
    """Установленные русская и английская раскладки и таблица клавиш между ними."""
    ru: int = 0
    en: int = 0
    keymap: KeyMap | None = None
    keymap_from: str = ""   # windows — ToUnicodeEx; default — запасная таблица ЙЦУКЕН/QWERTY
    reason: str = ""        # почему исправление раскладки выключено

    @property
    def ok(self) -> bool:
        return bool(self.ru and self.en)

    def hkl(self, cyrillic: bool) -> int:
        return self.ru if cyrillic else self.en


def layout_pair() -> LayoutPair:
    """Найти установленные русскую и английскую раскладки. Нет любой из них — pair.ok == False и причина."""
    try:
        hkls = installed_layouts()
        ru = next((h for h in hkls if primary_lang(h) == LANG_RUSSIAN), 0)
        en_all = [h for h in hkls if primary_lang(h) == LANG_ENGLISH]
        en = next((h for h in en_all if h & 0xFFFF == 0x0409), en_all[0] if en_all else 0)
    except Exception as e:  # noqa: BLE001 — WinAPI недоступен: исправление раскладки просто выключено
        log.exception("не получил список раскладок")
        return LayoutPair(reason=f"список раскладок недоступен: {e}")
    if not ru or not en:
        miss = " и ".join(n for n, h in (("русской", ru), ("английской", en)) if not h)
        return LayoutPair(ru=ru, en=en, reason=f"в Windows нет {miss} раскладки")
    km = None
    try:
        km = build_keymap(en, ru)
    except Exception:
        log.exception("таблица клавиш из ToUnicodeEx не построилась")
    if km is None:
        log.warning("таблица клавиш из Windows неполная — беру запасную ЙЦУКЕН/QWERTY")
    return LayoutPair(ru=ru, en=en, keymap=km or KeyMap(), keymap_from="windows" if km else "default")


def switch_layout(hkl: int, fg=None, top: bool = False) -> bool:
    """Попросить окно сменить раскладку (WM_INPUTLANGCHANGEREQUEST): окно с фокусом, а top — верхнего уровня.

    Только отправляет просьбу; сработала ли она, видно по focus_hkl() через ~150 мс. → принята ли в очередь.
    """
    try:
        target = (fg or user32.GetForegroundWindow()) if top else focus_target(fg)
        return bool(user32.PostMessageW(target, 0x0050, 0, hkl))
    except Exception:
        log.exception("не переключил раскладку")
        return False


def send_text(text: str, gap_s: float = 0.002, extra: int = MAGIC):
    """Напечатать текст в активное окно как юникод-нажатия (помечены MAGIC).

    По символу с паузой: новый Блокнот (WinUI) путает и теряет символы, если прислать
    длинную вставку одной пачкой SendInput (проверено 01.10.2026: «но иногда» → «нннннногда»).
    Пауза — с таймером 1 мс: в Python 3.10 sleep(0.002) иначе спит тик системного таймера ~15.6 мс.
    """
    fine = bool(gap_s) and len(text) > 1
    if fine:
        winmm.timeBeginPeriod(1)
    try:
        _send_chars(text, gap_s, extra)
    finally:
        if fine:
            winmm.timeEndPeriod(1)


def _send_chars(text: str, gap_s: float, extra: int):
    for i, ch in enumerate(text):
        data = ch.encode("utf-16-le")  # суррогатная пара (эмодзи) уходит одним пакетом
        units = [int.from_bytes(data[k:k + 2], "little") for k in range(0, len(data), 2)]
        arr = (INPUT * (len(units) * 2))()
        for k, u in enumerate(units):
            for j, flags in enumerate((0x0004, 0x0004 | 0x0002)):  # KEYEVENTF_UNICODE [| KEYUP]
                inp = arr[k * 2 + j]
                inp.type = 1
                inp.ki = KEYBDINPUT(0, u, flags, 0, extra)
        sent = user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
        if sent != len(arr):
            log.warning("SendInput: отправлено %s из %s (err %s)", sent, len(arr), ctypes.get_last_error())
        if gap_s and i < len(text) - 1:
            time.sleep(gap_s)


@dataclass
class Shared:
    """Состояние, которое колбэк хука читает без блокировок."""
    enabled: bool = True
    visible: bool = False   # подсказка показана
    shown_seq: int = -1     # для какого события она посчитана
    accept_right: bool = False
    variants: int = 0       # сколько вариантов длинной подсказки можно листать
    shown_id: int = 0       # какая именно подсказка на плашке: её и вставляем по Tab
    inserting: bool = False  # идёт вставка: нажатия откладываем, чтобы они не влезли в середину текста
    undo_armed: bool = False  # только что сработала автозамена раскладки: ближайший Backspace — отмена, а не стирание
    layout_gate: bool = False  # автозамена раскладки включена: после разделителя придержать ввод до решения
    gated: bool = False        # ввод придержан разделителем; отпускает главный поток (release_held)
    hkl_override: int = 0      # раскладка, на которую только что переключили окно (оно её ещё не применило)
    hkl_override_until: float = 0.0
    layout_hotkey: str = "none"  # ручная клавиша раскладки: pause | scroll_lock | double_shift | none


class Hook:
    def __init__(self, events: queue.Queue, shared: Shared):
        self.events = events
        self.shared = shared
        self.seq = 0
        self._swallow_up: set[int] = set()
        self._mask_alt = False  # Alt использован нашим сочетанием — его отпускание маскируем
        self._held: list[tuple] = []  # нажатия во время вставки: ("text", s) | ("vk", vk, scan, extended)
        self._held_lock = threading.Lock()
        self._shift_down = False   # Shift нажат; пока _shift_clean, других клавиш до его отпускания не было
        self._shift_clean = False
        self._last_tap = 0.0       # когда был предыдущий чистый тап Shift (для double_shift)
        # состояние CapsLock ведём сами: GetKeyState в потоке хука не обновляется
        self.caps = bool(user32.GetKeyState(VK_CAPITAL) & 1)
        self._thread_id = 0
        self._kb_proc = HOOKPROC(self._on_kb)
        self._ms_proc = HOOKPROC(self._on_mouse)
        self._ready = threading.Event()

    def _emit(self, *ev):
        self.seq += 1
        self.events.put((self.seq,) + ev)

    # --- клавиатура ---
    def _on_kb(self, code, wparam, lparam):
        if code == 0:
            kb = KBDLLHOOKSTRUCT.from_address(lparam)
            if kb.dwExtraInfo != MAGIC:
                try:
                    if self._handle_key(kb, wparam):
                        return 1
                except Exception:
                    log.exception("ошибка в хуке клавиатуры")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _handle_key(self, kb: KBDLLHOOKSTRUCT, wparam) -> bool:
        vk = kb.vkCode
        if wparam in (WM_KEYUP, WM_SYSKEYUP):
            if vk in self._swallow_up:
                self._swallow_up.discard(vk)
                return True
            if self._mask_alt and vk in (VK_MENU, VK_LMENU, VK_RMENU):
                self._mask_alt = False
                masked_alt_up(vk)
                return True
            if vk in SHIFTS:
                self._shift_up()
            return False
        if vk == VK_CAPITAL:
            self.caps = not self.caps
        sh = self.shared
        if vk in SHIFTS:
            if not self._shift_down:
                self._shift_down = self._shift_clean = True
            return False
        self._shift_clean = False  # другая клавиша между нажатием и отпусканием Shift — это не тап
        self._last_tap = 0.0
        ctrl, alt = _down(VK_CONTROL), _down(VK_MENU)
        shift, win = _down(VK_SHIFT), _down(VK_LWIN) or _down(VK_RWIN)
        if vk == HOTKEY_VK.get(sh.layout_hotkey) and sh.enabled and not (ctrl or alt or win):
            self._swallow_up.add(vk)
            self._emit("layout_key", vk)  # ручной перевод раскладки: Pause / Scroll Lock
            return True
        if vk in MODIFIERS:
            return False
        if self.shared.inserting and kb.dwExtraInfo != REPLAY and not (ctrl or alt or win):
            if self._hold(kb, vk, shift):
                return True
        if sh.undo_armed and not (vk == VK_BACK and not (ctrl or alt or win)):
            sh.undo_armed = False  # любая другая клавиша снимает отмену автозамены
        if ctrl and alt and vk == VK_F12:
            self._emit("toggle")
            return True
        accept_vk = VK_RIGHT if sh.accept_right else VK_TAB
        if vk == accept_vk and not (ctrl or alt or shift or win):
            if sh.enabled and sh.visible and sh.shown_seq == self.seq:
                sh.visible = False
                self._swallow_up.add(vk)
                self._emit("accept", vk, sh.shown_id)
                return True
        plain_ctrl = ctrl and not (alt or shift or win)
        if vk == VK_RIGHT and plain_ctrl and sh.enabled and sh.visible and sh.shown_seq == self.seq:
            sh.visible = False
            self._swallow_up.add(vk)
            self._emit("accept_word", vk, sh.shown_id)
            return True
        if (vk in (VK_DOWN, VK_UP) and alt and not (ctrl or shift or win) and sh.enabled and sh.visible
                and sh.variants > 1 and sh.shown_seq == self.seq):
            self._swallow_up.add(vk)
            self._mask_alt = True
            self._emit("variant", 1 if vk == VK_DOWN else -1)
            return True
        if vk == VK_PACKET:
            self._emit("char", chr(kb.scanCode))
            return False
        if vk == VK_ESCAPE:
            self._emit("dismiss")
        elif vk == VK_BACK:
            if sh.undo_armed and sh.enabled:
                sh.undo_armed = False
                self._swallow_up.add(vk)
                self._emit("layout_undo")  # Backspace сразу после автозамены раскладки: вернуть как было
                return True
            self._emit("reset", "ctrl-bs") if (ctrl or alt) else self._emit("bs")
        elif vk == VK_RETURN:
            self._emit("enter")
        elif vk in NAV or vk == VK_TAB or win:
            self._emit("reset", "nav")
        elif (ctrl or alt) and not (ctrl and alt):
            self._emit("reset", "shortcut")
        else:
            ch = self._translate(vk, kb.scanCode, shift, ctrl and alt)
            if ch:
                if sh.layout_gate and sh.enabled and (ch.isspace() or ch in GATE_DELIMS):
                    # слово закрыто: пока главный поток решает про автозамену, следующие нажатия придерживаем —
                    # иначе они попадут в поле раньше стирания, и сотрётся не то
                    with self._held_lock:
                        if not sh.inserting:
                            sh.inserting = sh.gated = True
                self._emit("char", ch)
            elif ctrl and alt:
                self._emit("reset", "shortcut")
        return False

    def _shift_up(self):
        """Отпустили Shift: два чистых тапа подряд за DOUBLE_SHIFT_S — ручной перевод раскладки (double_shift)."""
        clean, self._shift_down = self._shift_clean, False
        if not clean or self.shared.layout_hotkey != "double_shift" or not self.shared.enabled:
            self._last_tap = 0.0
            return
        now = time.perf_counter()
        if self._last_tap and now - self._last_tap <= DOUBLE_SHIFT_S:
            self._last_tap = 0.0
            self._emit("layout_key", 0)  # Shift в окно уже ушёл: событие без клавиши, возвращать нечего
        else:
            self._last_tap = now

    def _hold(self, kb: KBDLLHOOKSTRUCT, vk: int, shift: bool) -> bool:
        """Во время вставки: отложить нажатие (символ — текстом, остальное — клавишей). True — проглотить."""
        if vk == VK_PACKET:
            item = ("text", chr(kb.scanCode))
        elif self.shared.gated and vk not in NAV:
            # после разделителя: клавишей, а не буквой — если раскладку сейчас исправят, буква будет уже в новой
            item = ("key", vk, kb.scanCode, bool(kb.flags & 1), shift)
        else:
            special = vk in NAV or vk in (VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE)
            ch = "" if special else self._translate(vk, kb.scanCode, shift, False)
            item = ("text", ch) if ch else ("vk", vk, kb.scanCode, bool(kb.flags & 1))  # LLKHF_EXTENDED
        with self._held_lock:
            if not self.shared.inserting:
                return False
            self._held.append(item)
        self._swallow_up.add(vk)
        return True

    def release_held(self):
        """Конец вставки: повторить отложенные нажатия по порядку (пока повторяем, новые тоже откладываются)."""
        while True:
            with self._held_lock:
                items, self._held = self._held, []
                if not items:
                    self.shared.inserting = self.shared.gated = False
                    return
            for it in items:
                if it[0] == "text":
                    send_text(it[1], gap_s=0, extra=REPLAY)
                elif it[0] == "key":
                    send_key_shifted(it[1], it[2], it[3], it[4])
                else:
                    send_key(it[1], it[2], it[3])

    def _translate(self, vk: int, scan: int, shift: bool, altgr: bool) -> str:
        sh = self.shared
        if sh.hkl_override and time.perf_counter() < sh.hkl_override_until:
            hkl = sh.hkl_override  # окну только что сменили раскладку, а оно ещё не обработало просьбу
        else:
            hkl = focus_hkl()  # раскладка потока с фокусом, а не окна верхнего уровня
        state = (ctypes.c_ubyte * 256)()
        if shift:
            state[VK_SHIFT] = 0x80
        if altgr:
            state[VK_CONTROL] = state[VK_MENU] = 0x80
        if self.caps:
            state[VK_CAPITAL] = 0x01
        buf = ctypes.create_unicode_buffer(8)
        # флаг 0x4: не трогать состояние клавиатуры (иначе ломаются мёртвые клавиши)
        n = user32.ToUnicodeEx(vk, scan, ctypes.cast(state, ctypes.c_char_p), buf, 8, 0x4, hkl)
        if n <= 0:
            return ""
        s = buf.value[:n]
        return s if s.isprintable() else ""

    # --- мышь: любой клик сбивает позицию каретки ---
    def _on_mouse(self, code, wparam, lparam):
        if code == 0 and wparam in MOUSE_DOWN:
            try:
                self._last_tap = 0.0
                self.shared.undo_armed = False
                self._emit("reset", "mouse")
            except Exception:
                log.exception("ошибка в хуке мыши")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    # --- поток с циклом сообщений ---
    def start(self):
        threading.Thread(target=self._run, name="hook", daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
        ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
        if not kb or not ms:
            log.error("SetWindowsHookEx не удался: %s", ctypes.get_last_error())
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(kb)
        user32.UnhookWindowsHookEx(ms)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
