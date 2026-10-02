"""Где каретка и не поле ли это пароля.

1) GetGUIThreadInfo — классические Win32-поля (Блокнот, Word, многие Qt-приложения).
2) UI Automation (TextPattern2.GetCaretRange) — Chrome/Electron/WinUI.
Вызывать из потока, где инициализирован COM (главный поток tk).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging

log = logging.getLogger(__name__)
user32 = ctypes.WinDLL("user32", use_last_error=True)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]

ES_PASSWORD = 0x20


def _gti(hwnd) -> GUITHREADINFO | None:
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    return info if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) else None


class CaretLocator:
    def __init__(self):
        self._uia = None
        self._mod = None
        try:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as m
            self._mod = m
            self._uia = comtypes.CoCreateInstance(m.CUIAutomation._reg_clsid_, interface=m.IUIAutomation,
                                                  clsctx=comtypes.CLSCTX_INPROC_SERVER)
            try:  # не виснуть на зависших приложениях
                u2 = self._uia.QueryInterface(m.IUIAutomation2)
                u2.ConnectionTimeout = 400
                u2.TransactionTimeout = 400
            except Exception:
                pass
        except Exception:
            log.warning("UI Automation недоступна, только GetGUIThreadInfo", exc_info=True)

    # --- позиция ---
    def locate(self, hwnd) -> tuple[int, int, int, str] | None:
        """→ (x, y, высота строки, источник) в экранных пикселях."""
        info = _gti(hwnd)
        if info and info.hwndCaret:
            r = info.rcCaret
            h = r.bottom - r.top
            if h > 0:
                pt = wt.POINT(r.right, r.top)
                user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
                if not (pt.x == 0 and pt.y == 0):
                    return pt.x, pt.y, h, "gti"
        return self._uia_caret()

    def _uia_caret(self):
        if not self._uia:
            return None
        m = self._mod
        try:
            el = self._uia.GetFocusedElement()
            if not el:
                return None
            rng = None
            pat = el.GetCurrentPattern(m.UIA_TextPattern2Id)
            if pat:
                tp2 = pat.QueryInterface(m.IUIAutomationTextPattern2)
                _active, rng = tp2.GetCaretRange()
            else:
                pat = el.GetCurrentPattern(m.UIA_TextPatternId)
                if pat:
                    sel = pat.QueryInterface(m.IUIAutomationTextPattern).GetSelection()
                    if sel and sel.Length:
                        rng = sel.GetElement(0)
            if rng is None:
                return None
            rects = rng.GetBoundingRectangles()
            if rects and len(rects) >= 4 and rects[3] > 0:
                x, y, w, h = rects[0], rects[1], rects[2], rects[3]
                return int(x + w), int(y), int(h), "uia"
            # пустой диапазон: берём предыдущий символ и его правый край
            r2 = rng.Clone()
            r2.MoveEndpointByUnit(m.TextPatternRangeEndpoint_Start, m.TextUnit_Character, -1)
            rects = r2.GetBoundingRectangles()
            if rects and len(rects) >= 4 and rects[-1] > 0:
                x, y, w, h = rects[-4:]
                return int(x + w), int(y), int(h), "uia-prev"
        except Exception as e:
            log.debug("UIA caret: %s", e)
        return None

    # --- контекст ---
    def text_before_caret(self, max_chars: int = 200) -> str | None:
        """Текст перед курсором в поле с фокусом (UIA). None — поле не отдаёт текст."""
        if not self._uia:
            return None
        m = self._mod
        try:
            el = self._uia.GetFocusedElement()
            if not el or el.CurrentIsPassword:
                return None
            rng = None
            pat = el.GetCurrentPattern(m.UIA_TextPattern2Id)
            if pat:
                _active, rng = pat.QueryInterface(m.IUIAutomationTextPattern2).GetCaretRange()
            else:
                pat = el.GetCurrentPattern(m.UIA_TextPatternId)
                if pat:
                    sel = pat.QueryInterface(m.IUIAutomationTextPattern).GetSelection()
                    if sel and sel.Length:
                        rng = sel.GetElement(0)
            if rng is None:
                return None
            r = rng.Clone()
            r.MoveEndpointByRange(m.TextPatternRangeEndpoint_End, rng, m.TextPatternRangeEndpoint_Start)
            r.MoveEndpointByUnit(m.TextPatternRangeEndpoint_Start, m.TextUnit_Character, -max_chars)
            text = r.GetText(max_chars)
            # \xa0 — Chromium ставит неразрывный пробел в конец редактируемого текста
            return text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
        except Exception as e:
            log.debug("UIA text: %s", e)
        return None

    # --- пароль ---
    def is_password(self, hwnd) -> bool:
        info = _gti(hwnd)
        if info and info.hwndFocus:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(info.hwndFocus, cls, 64)
            if "edit" in cls.value.lower() and user32.GetWindowLongW(info.hwndFocus, -16) & ES_PASSWORD:
                return True
        if self._uia:
            try:
                el = self._uia.GetFocusedElement()
                return bool(el and el.CurrentIsPassword)
            except Exception:
                pass
        return False
