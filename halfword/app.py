"""Склейка: хук → буфер текста → предсказатель → плашка у каретки; иконка в трее."""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser

from . import config as C
from . import panel
from .caret import CaretLocator, SelectionTooLong
from .layout import LayoutDetector
from .llm import LLM, on_ac_power
from .model import DOC_WEIGHT, WORD_CH, WORD_RE, BaseModel, Predictor, Suggestion, UserModel, parse_context
from .overlay import Overlay
from .stats import Stats
from .trainer import Trainer
from .usage import MissWatch, Usage, layout_info, place
from .winhook import (GATE_DELIMS, LANG_ENGLISH, LANG_RUSSIAN, MAGIC, VK_BACK, VK_RIGHT, VK_TAB, Hook, Shared,
                      focus_hkl, layout_pair, primary_lang, process_name, send_key, send_text, switch_layout,
                      user32, window_header)

log = logging.getLogger("halfword")
DEBUG = os.environ.get("HALFWORD_DEBUG") == "1"  # в лог пишется и текст подсказок — только для отладки
WORD_END_RE = re.compile(rf"[{WORD_CH}]$")
SHOW_DELAY_MS = 20     # потом уточняем позицию, когда приложение передвинет каретку
SAVE_EVERY_S = 120
BUF_MAX = 2000
SYNC_CHARS = 64        # сколько текста поля сверять с концом буфера после нажатия
SYNC_LAG = 3           # поле может отставать от буфера на столько символов (приложение ещё не вставило)
SYNC_GIVE_UP = 5       # столько расхождений подряд — поле отдаёт не тот текст, в этом окне больше не сверяем
CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAYOUT_TAIL = 400      # сколько символов текста отдаём детектору раскладки (смотрит на последние слова)
SWITCH_CHECK_MS = 150  # через сколько проверить, что окно сменило раскладку
SWITCH_OVERRIDE_S = 0.4  # столько хук переводит нажатия в новой раскладке, не спрашивая окно
MANUAL_UNDO_S = 3.0    # повторная ручная клавиша за это время откатывает перевод
SELECTION_MAX = 2000   # выделение длиннее ручная клавиша не переводит: целиком его из поля не прочитать
LAYOUT_STATE_MAX = 500  # столько слов со счётчиком отказов храним (самые давние забываем)
POLL_FAST_MS = 8       # шаг главного цикла, пока печатают
POLL_STEPS = ((2.0, POLL_FAST_MS), (5.0, 25), (15.0, 50))  # секунд без нажатий → шаг, мс
POLL_IDLE_MS = 100     # шаг в простое: событие из хука или потока будит цикл сразу (WakeQueue)


def poll_delay(idle_s: float) -> int:
    """Шаг главного цикла: часто, пока печатают, и всё реже в простое."""
    for limit, ms in POLL_STEPS:
        if idle_s < limit:
            return ms
    return POLL_IDLE_MS


class WakeQueue(queue.Queue):
    """Очередь, которая будит главный цикл: положивший (хук, поток) не ждёт следующего круга _poll и не трогает tk."""

    def __init__(self, wake: threading.Event):
        super().__init__()
        self.wake = wake

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self.wake.set()


def typed_after(buf: str, buf_off: int, text: str, off: int) -> str | None:
    """Что напечатано после text, если буфер его продолжает; None — текст уже другой.

    text и буфер заданы с абсолютным смещением начала: буфер — скользящее окно, его начало уезжает,
    поэтому buf.startswith(text) после BUF_MAX символов перестал бы совпадать на первом же нажатии.
    """
    cut = off + len(text) - buf_off  # где кончается text в нынешнем буфере
    if buf_off < off or not 0 <= cut <= len(buf) or buf[:cut] != text[buf_off - off:]:
        return None
    return buf[cut:]


def field_agrees(buf: str, field: str) -> bool:
    """Конец буфера совпадает с текстом поля перед курсором (поле может отставать на SYNC_LAG символов)."""
    for lag in range(SYNC_LAG + 1):
        b = buf[:len(buf) - lag]
        n = min(len(b), len(field))
        if not n or b[-n:] == field[-n:]:
            return True
    return False


def accept_chunk(insert: str) -> str:
    """Ctrl+→: пробелы перед словом + слово со знаками препинания."""
    m = re.match(r"\s*\S+", insert)
    return m.group(0) if m else insert


def setup_logging():
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(C.DATA_DIR / "halfword.log", maxBytes=1_000_000,
                                             backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    if sys.stderr:
        root.addHandler(logging.StreamHandler())


def set_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def single_instance() -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    single_instance.handle = k32.CreateMutexW(None, False, "Local\\halfword_single")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


class App:
    # значения по умолчанию для полей, которые появились позже (тесты собирают App через __new__)
    gaps: list = []
    learned = None
    panel = None
    layout = None          # LayoutDetector; пока грузятся словари — None, работает прежнее правило в слове
    pair = None            # winhook.LayoutPair: установленные русская и английская раскладки
    layout_floor = 0       # абсолютная позиция: текст раньше неё детектору раскладки не отдаём (отменено, отклонено)
    undo = None            # автозамена, которую ещё можно отменить Backspace
    manual = None          # последний ручной перевод — повторная клавиша откатит
    miss = None            # MissWatch: пропуски раскладки для журнала
    layout_state = None    # счётчики отмен по словам (data/layout_state.json)
    _poll_job = None       # запланированный круг _poll (after id)
    _poll_ms = POLL_FAST_MS
    _activity = 0.0        # monotonic последнего события хука

    def __init__(self):
        self.cfg = C.Config.load()
        self._wake = threading.Event()
        self.cmds: queue.Queue = WakeQueue(self._wake)  # команды из трея и потоков
        self.trainer = Trainer(lambda ev: self.cmds.put(lambda: self._on_train(ev)))
        self._base_seq = 0        # какая загрузка базовой модели последняя (старые ответы выбрасываем)
        self.predictor = Predictor(self._load_base(), UserModel.load(C.USER_MODEL))
        self._apply_predictor_cfg()
        self.predictor.user.decay(time.time())  # набранное руками тает: полураспад год
        self.stats = Stats(C.STATS_FILE)
        self.usage = Usage(C.USAGE_FILE)
        self.events: queue.Queue = WakeQueue(self._wake)
        self.shared = Shared(enabled=self.cfg.enabled, accept_right=self.cfg.accept_key == "right",
                             layout_hotkey=self.cfg.layout_hotkey)
        self.hook = Hook(self.events, self.shared)
        self.root = tk.Tk()
        self.root.withdraw()
        self.caret = CaretLocator()
        self.overlay = Overlay(self.root, self.cfg.font, theme=self.cfg.overlay_theme)
        self.key_hint = "→" if self.shared.accept_right else "Tab"
        self.buf = ""
        self.buf_off = 0      # абсолютная позиция начала buf (буфер — скользящее окно BUF_MAX)
        self.history: dict[int, Suggestion] = {}  # показанные подсказки по id: Tab вставляет ту, что была на плашке
        self.shown_id = 0
        self.pending_reload = False  # перечитать текст поля перед курсором (после клика, стрелок, смены окна)
        self.sync_due = False        # после нажатия сверить буфер с полем: раскладка, автозамена
        self.sync_ok = True          # поле отдаёт честный текст (иначе в этом окне не сверяем)
        self.sync_miss = 0
        self.fg = None
        self.exe = ""
        self.blocked = False
        self.password = False
        self.need_check = True
        self.dismissed = False
        self.suggestion = None
        self.last_seq = 0
        # последняя точно найденная позиция каретки: (x, y, h, длина буфера в тот момент)
        self.anchor = None
        self.cw_ratio = 0.5   # ширина символа / высота строки, подстраивается по ходу
        self.last_save = time.time()
        self.tray = None
        cfg = self.cfg
        self.llm = LLM(cfg.llm_model, cfg.llm_threads, ctx=cfg.llm_ctx)
        self.llm_installing = None  # строка прогресса, пока скачивается модель
        self.llm.configure(keep_long=cfg.llm_prompt_chars, long_tokens=cfg.llm_long_tokens,
                           long_words=cfg.llm_long_words, long_min_tok_p=cfg.llm_long_min_tok_p,
                           variants=cfg.llm_variants)
        self.llm_res = None       # последний ответ LLM в режиме набора
        self.variants = []        # варианты длинной подсказки (режим паузы) для одного текста
        self.vi = 0               # какой вариант показан
        self.partial = False      # подсказку уже начали принимать по словам
        self.last_key = 0.0       # для выгрузки LLM по простою
        self.gaps: list[float] = []  # паузы между последними нажатиями, мс — быстрый набор
        self.learned = None       # (абс. конец слова, c2, c1, слово) последнего выученного — откатить, если стёрли
        self.panel = None
        self.on_ac = True
        self.power_checked = 0.0
        self.llm_was_ready = False
        # трей
        self.pause_until = 0.0    # подсказки на паузе до этого времени (в config.enabled не пишется)
        self.typed_exe = ""       # последняя программа, где печатали (self.exe прыгает на трей при клике)
        self._tick_at = 0.0
        self._train_check = 0.0
        self._llm_warned = False
        self._icons: dict = {}
        self._icon_state = None
        self._menu_key = None
        self._pair_check = 0.0
        self._init_layout()

    def _apply_predictor_cfg(self):
        """Пороги и переключатели предсказателя из config (при запуске и после правки в панели)."""
        cfg, p = self.cfg, self.predictor
        for k in C.PREDICTOR_KEYS:
            setattr(p, k, getattr(cfg, k))
        p.min_insert = cfg.min_insert_chars
        p.next_min_tri = cfg.next_min_tri
        p.doc_weight = DOC_WEIGHT if cfg.doc_cache else 0.0
        p.layout_fix = self._layout_mode() != "off"
        if self.layout:
            self.layout.never = {w.lower() for w in cfg.layout_never}
            self.layout._cache.clear()  # в кэше слова с прежними исключениями
        if getattr(self, "shared", None) is not None:
            self.shared.layout_hotkey = getattr(cfg, "layout_hotkey", "none")
        p.snippets = {k.lower(): v for k, v in cfg.snippets.items()}
        p.banned = {w.lower() for w in cfg.banned_words}
        if hasattr(self, "layout"):
            self._update_gate()

    def _load_base(self) -> BaseModel:
        """Модель с диска; нет или битая — пустая, а обучение идёт в отдельном процессе."""
        if not C.BASE_MODEL.exists():
            self.trainer.apply_pending()  # осталась отложенная — берём её
        if C.BASE_MODEL.exists():
            try:
                return BaseModel.load(C.BASE_MODEL)
            except Exception:
                log.exception("base_model.pkl не читается — обучаю заново")
        else:
            log.info("модели нет — обучаю на текстах из источников")
        self.trainer.start("first")
        base = BaseModel()
        base._index()
        return base

    # --- исправление раскладки ---
    def _layout_mode(self) -> str:
        return getattr(self.cfg, "layout_mode", "off")

    def _update_gate(self):
        """Хук придерживает ввод после разделителя только при автозамене и готовом детекторе."""
        if getattr(self, "shared", None) is None:
            return  # ещё в __init__: выставится, когда загрузится детектор
        self.shared.layout_gate = (self._layout_mode() == "auto" and self.layout is not None
                                   and self.pair is not None and self.pair.ok and not self._auto_off())

    def _auto_off(self) -> bool:
        """Автозамены в этом окне нет: программа в списке без автозамены, чёрный список или поле пароля."""
        return (getattr(self, "blocked", False) or getattr(self, "password", False)
                or getattr(self, "exe", "") in getattr(self.cfg, "layout_auto_off", ()))

    def _init_layout(self):
        """Раскладки из Windows и детектор: словари грузятся ~1.4 с, поэтому в потоке; объект живёт до выхода."""
        try:
            st = json.loads(C.LAYOUT_STATE_FILE.read_text(encoding="utf-8"))
            self.layout_state = st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            self.layout_state = {}
        undo = self.layout_state.get("undo")
        if isinstance(undo, dict):  # ключи до T-059 были самим текстом — их не храним
            self.layout_state["undo"] = {k: v for k, v in undo.items() if re.fullmatch(r"[0-9a-f]{16}", k)}
        self.pair = layout_pair()
        if not self.pair.ok:
            log.warning("исправление раскладки выключено: %s", self.pair.reason)
            return
        pair, never = self.pair, list(self.cfg.layout_never)

        def work():
            try:
                det = LayoutDetector(known=self.predictor.word_count, never=never, keymap=pair.keymap)
            except Exception:
                log.exception("не загрузил словари раскладки")
                return
            self.cmds.put(lambda: self._set_layout(det))

        threading.Thread(target=work, daemon=True, name="layout-load").start()

    def _set_layout(self, det: LayoutDetector):
        self.layout = self.predictor.layout = det
        self.miss = MissWatch(det.convert_text, lambda text: det.detect(text) is None)
        det.never = {w.lower() for w in self.cfg.layout_never}
        det._cache.clear()
        self._update_gate()
        log.info("детектор раскладки готов (таблица клавиш: %s)", self.pair.keymap_from if self.pair else "?")

    def layout_status(self) -> str:
        """Для панели: почему исправление раскладки не работает или что оно работает."""
        if self.pair is None:
            return ""
        if not self.pair.ok:
            return "выключено: " + self.pair.reason
        if self.layout is None:
            return "словари загружаются"
        return "работает" + ("" if self.pair.keymap_from == "windows" else " (запасная таблица клавиш)")

    def _layout_blocked(self) -> bool:
        """Сейчас активна не русская и не английская раскладка (например, грузинская): детектор молчит."""
        if self.pair is None or not self.pair.ok:
            return False
        return primary_lang(focus_hkl(self.fg)) not in (LANG_RUSSIAN, LANG_ENGLISH)

    def _layout_text(self) -> str | None:
        """Текст для детектора: после «нижней границы» (отменённое и отклонённое не берём), не длиннее LAYOUT_TAIL.

        None — детектора нет (грузится), Predictor применит прежнее правило; "" — раскладку сейчас не проверяем.
        """
        if self.layout is None:
            return None
        if self._layout_blocked():
            return ""
        return self.buf[max(0, self.layout_floor - self.buf_off):][-LAYOUT_TAIL:]

    def _later(self, ms: int, fn, *args):
        self.root.after(ms, fn, *args)

    def _switch(self, cyr: bool, row: dict | None = None):
        if self.pair is not None and self.pair.ok:
            self._switch_hkl(self.pair.hkl(cyr), row)

    def _switch_hkl(self, hkl: int, row: dict | None = None):
        """Попросить окно с фокусом сменить раскладку и через SWITCH_CHECK_MS проверить, что сменилась.

        Не сменилась — повтор в окно верхнего уровня и ещё одна проверка; итог — row["switch_ok"].
        """
        if not hkl:
            return
        if focus_hkl(self.fg) & 0xFFFFFFFF == hkl & 0xFFFFFFFF:
            if row is not None:
                row["switch_ok"] = True
            return
        switch_layout(hkl, self.fg)
        # окно применит просьбу позже, а придержанные нажатия повторяются сразу: хук переводит их уже в новой
        self.shared.hkl_override, self.shared.hkl_override_until = hkl, time.perf_counter() + SWITCH_OVERRIDE_S
        self._later(SWITCH_CHECK_MS, self._check_switch, hkl, 0, row)

    def _check_switch(self, hkl: int, attempt: int, row: dict | None):
        if focus_hkl(self.fg) & 0xFFFFFFFF == hkl & 0xFFFFFFFF:
            ok = True
        elif attempt == 0:
            switch_layout(hkl, self.fg, top=True)
            self._later(SWITCH_CHECK_MS, self._check_switch, hkl, 1, row)
            return
        else:
            ok = False
            log.warning("раскладка не переключилась в %s", self.exe or "?")
        if row is not None:
            row["switch_ok"] = ok

    def _disarm_undo(self):
        self.undo = None
        self.shared.undo_armed = False

    def _apply_layout(self, fix, how: str, before_insert=None) -> tuple[dict, int]:
        """Стереть fix.erase символов, вставить fix.insert, переключить раскладку. → (строка журнала, прежняя HKL).

        Раскладку просим сменить до вставки: в конце вставки хук повторяет придержанные нажатия, и они должны
        попасть в окно уже в новой раскладке. before_insert(prev) — успеть взвести отмену до этих повторов.
        """
        prev = focus_hkl(self.fg) if self.pair is not None and self.pair.ok else 0
        self._hide()
        info = layout_info(fix.original, fix.to_cyr, fix.words, self._layout_mode(), how)
        row = self.usage.layout_done("accepted", info, self.exe)
        self.usage.reset("layout")
        self.stats.layout_fixed(fix.erase, len(fix.insert))
        self._switch(fix.to_cyr, row)
        self.buf = self.buf[:max(0, len(self.buf) - fix.erase)]
        self._append(fix.insert)
        self.sync_due = self.pending_reload = False  # поле ещё обрабатывает замену: сверять с ним рано
        self.suggestion = None
        if before_insert:
            before_insert(prev)
        self._insert(fix.insert, erase=fix.erase)
        return row, prev

    def _layout_auto(self, ch: str):
        """Автозамена: слово закрыто разделителем, детектор уверен — исправить и взвести отмену Backspace."""
        if (self._layout_mode() != "auto" or self.layout is None or self.pair is None or not self.pair.ok
                or self._auto_off() or not (ch.isspace() or ch in GATE_DELIMS)):
            return
        if self.pending_reload:
            self._sync_field()  # контекст после клика: сначала настоящий текст поля
        text = self._layout_text()
        fix = self.layout.detect(text) if text else None
        if not fix or not (fix.auto and fix.closed):
            return
        field = self.caret.text_before_caret(fix.erase + SYNC_LAG + 1)
        if field and not field_agrees(self.buf, field):
            log.debug("автозамена раскладки пропущена: поле не совпадает с буфером")
            return
        if self.shared.gate_expired:
            return  # сторож гейта уже отпустил набранное после разделителя: стирать вслепую нельзя
        def arm(prev):  # Backspace, придержанный с пробела, повторится в конце вставки — он уже отмена
            self.undo = {"fix": fix, "prev": prev or self.pair.hkl(not fix.to_cyr), "end": self._end()}
            self.shared.undo_armed = True

        self._apply_layout(fix, "auto", before_insert=arm)
        log.debug("автозамена раскладки (%s слов)", fix.words)

    def _layout_undo(self) -> bool:
        """Backspace, который хук проглотил после автозамены: вернуть написанное и прежнюю раскладку."""
        u, self.undo = self.undo, None
        self.shared.undo_armed = False
        if u is None or u["end"] != self._end():
            send_key(VK_BACK, extra=MAGIC)  # отменять уже нечего — это был обычный Backspace
            return self._handle("bs")
        fix = u["fix"]
        self._hide()
        self._switch_hkl(u["prev"])  # до вставки: придержанные нажатия повторятся уже в прежней раскладке
        self.buf = self.buf[:max(0, len(self.buf) - len(fix.insert))]
        self._append(fix.original)
        self.layout_floor = self._end()  # отменённое детектор больше не трогает
        self.sync_due = False
        self._insert(fix.original, erase=len(fix.insert))
        info = layout_info(fix.original, fix.to_cyr, fix.words, self._layout_mode(), "auto")
        self.usage.layout_done("undone", info, self.exe)
        self.stats.layout_fixed(fix.erase, len(fix.insert), undo=True)
        self._count_reject(fix, info)
        return True

    def _never_words(self, original: str) -> list[str]:
        """Слова исправления в том виде, как их узнаёт детектор: у слова с , . ; ' [ ] в латинице это часть слова."""
        words = []
        for tok in original.split():
            core = self.layout.never_key(tok)
            if core and (len(core) >= 3 or len(original.split()) == 1) and core not in words:
                words.append(core)
        return words

    def _count_reject(self, fix, info: dict):
        """Второй отказ от исправления того же слова (отмена автозамены или Esc на плашке) — в исключения (layout_never).

        Счётчики хранятся по хэшу слова, а не по тексту, и не больше LAYOUT_STATE_MAX: набранное на диск не пишем.
        """
        key = hashlib.sha1("".join(fix.original.lower().split()).encode("utf-8")).hexdigest()[:16]
        if self.layout_state is None:
            self.layout_state = {}
        undo = self.layout_state.setdefault("undo", {})
        n = undo.pop(key, 0) + 1   # заново в конец: самые давние отказы забываются первыми
        undo[key] = n
        while len(undo) > LAYOUT_STATE_MAX:
            undo.pop(next(iter(undo)))
        if n >= 2:
            undo.pop(key)
            new = [w for w in self._never_words(fix.original) if w not in self.cfg.layout_never]
            if new:
                self.cfg.layout_never.extend(new)
                self.cfg.save()
                self.layout.never |= set(new)
                self.layout._cache.clear()
            self.usage.layout_done("never", info, self.exe)
            log.info("слово ушло в исключения раскладки (после второй отмены)")
        try:
            C.LAYOUT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            C.LAYOUT_STATE_FILE.write_text(json.dumps(self.layout_state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.exception("не сохранил layout_state.json")

    def _layout_key(self, vk: int) -> bool:
        """Ручная клавиша: повтор сразу после перевода откатывает; есть выделение — переводит его; иначе последнюю цепочку."""
        prev_manual, self.manual = self.manual, None
        det = self.layout
        if det is None or self.pair is None or not self.pair.ok or self.password:
            self._pass_through("layout_key", (vk,))
            return True
        mode = self._layout_mode()
        if prev_manual and time.time() - prev_manual["t"] <= MANUAL_UNDO_S and prev_manual["fg"] == self.fg:
            m = prev_manual
            self._hide()
            self._switch_hkl(m["prev"])  # до вставки, как и при исправлении
            self._insert(m["original"], erase=len(m["insert"]))
            self.buf = self.buf[:max(0, len(self.buf) - len(m["insert"]))]
            self._append(m["original"])
            self.layout_floor = self._end()
            self.sync_due = False
            info = layout_info(m["original"], m["to_cyr"], m["words"], mode, "hotkey")
            self.usage.layout_done("undone", info, self.exe)
            self.stats.layout_fixed(len(m["original"]), len(m["insert"]), undo=True)
            return True
        get_sel = getattr(self.caret, "selected_text", None)
        try:
            sel = get_sel(SELECTION_MAX) if get_sel else None  # нет UIA или поле не отдаёт текст — выделения нет
        except SelectionTooLong:
            log.info("выделение длиннее %s симв. или из нескольких кусков — раскладку не перевожу", SELECTION_MAX)
            return True  # набор поверх стёр бы непрочитанный остаток
        if sel:
            conv, to_cyr = det.convert_text(sel)
            if conv == sel:
                return True
            prev = focus_hkl(self.fg)
            self._hide()
            info = layout_info(sel, to_cyr, len(sel.split()), mode, "hotkey")
            row = self.usage.layout_done("accepted", info, self.exe)
            self.usage.reset("layout")
            self.stats.layout_fixed(len(sel), len(conv))
            self._switch(to_cyr, row)  # до вставки, как и при исправлении
            self._insert(conv)  # набор поверх выделения заменяет его
            self._reset_buf()
            self.pending_reload = True   # текст вокруг выделения неизвестен — перечитаем из поля
            self.sync_due = False
            self.manual = {"t": time.time(), "insert": conv, "original": sel, "to_cyr": to_cyr,
                           "words": len(sel.split()), "prev": prev, "fg": self.fg}
            return True
        text = self._layout_text()
        fix = det.convert_last(text) if text else None
        if fix is None or fix.insert == fix.original:
            return True
        _row, prev = self._apply_layout(fix, "hotkey")
        self.manual = {"t": time.time(), "insert": fix.insert, "original": fix.original, "to_cyr": fix.to_cyr,
                       "words": fix.words, "prev": prev, "fg": self.fg}
        return True

    def _pass_through(self, kind: str, args: tuple):
        """Клавишу проглотил хук, а действовать не нужно (выключено, чёрный список, пароль): вернуть её окну."""
        if kind == "layout_undo":
            send_key(VK_BACK, extra=MAGIC)
        elif kind == "layout_key" and args and args[0]:
            send_key(args[0], extra=MAGIC)

    def _miss_bs(self, removed: str):
        if self.miss is not None and removed and self._layout_mode() != "off":
            self.miss.bs(removed)

    def _miss_char(self, ch: str):
        if self.miss is None or self._layout_mode() == "off":
            return
        ev = self.miss.char(ch)
        if ev:
            self.usage.layout_missed(self.exe, ev["dir"], ev["len"])

    # --- цикл ---
    def run(self):
        self.hook.start()
        try:
            self.panel = panel.start(self)
            log.info("панель: http://127.0.0.1:%s", self.panel.url.split(":")[2].split("/")[0])
        except Exception:
            log.exception("веб-панель не запустилась")
        self._start_tray()
        log.info("запущено; вкл/выкл — Ctrl+Alt+F12 или иконка в трее")
        self.root.bind("<<HalfwordWake>>", self._wake_poll)
        self._poll_job = self.root.after(10, self._poll)
        threading.Thread(target=self._waker, daemon=True, name="poll-wake").start()
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    def _waker(self):
        """Поток: в очереди событие, а цикл спит длинным шагом — будим его виртуальным событием tk.

        Сам хук tk не трогает (вызов tk из чужого потока ждёт главный поток), он только взводит флаг."""
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._poll_ms <= POLL_FAST_MS:
                continue  # и так проснётся через 8 мс
            try:
                self.root.event_generate("<<HalfwordWake>>", when="tail")
            except RuntimeError:
                time.sleep(0.05)  # главный цикл ещё не запущен или уже остановлен
            except tk.TclError:
                return  # окно уничтожено — выход

    def _wake_poll(self, _ev=None):
        """Круг цикла сейчас, а не по долгому таймеру простоя."""
        if self._poll_job is None:
            return
        self.root.after_cancel(self._poll_job)
        self._poll()

    def _poll(self):
        self._poll_job = None
        changed = typed = False
        try:
            while True:
                ev = self.events.get_nowait()
                self.last_seq = self.shared.handling_seq = ev[0]
                try:
                    typed = self._handle(ev[1], *ev[2:]) or typed
                finally:
                    # решение по разделителю, на котором хук придержал ввод, принято — отпускаем; ранние события — нет
                    if self.shared.gated and ev[0] >= self.shared.gate_seq:
                        self.hook.release_held()
                if ev[1] == "char":
                    self.typed_exe = self.exe  # для пунктов трея «выключить в <программе>»
                changed = True
        except queue.Empty:
            pass
        try:
            while True:
                self.cmds.get_nowait()()
        except queue.Empty:
            pass
        if changed:
            self._show_now()
            self.root.after(SHOW_DELAY_MS, self._refresh, self.last_seq)
        if typed:  # текст изменился: генерацию по старому тексту бросаем, по новому — заново
            self.llm.cancel()
            self.root.after(self.cfg.llm_delay_ms, self._ask_llm, self.last_seq)
            self.root.after(self.cfg.llm_pause_ms, self._ask_long, self.last_seq)
        if time.time() - self.power_checked > 15:
            self._manage_llm()
        if self.llm.ready != self.llm_was_ready:
            self.llm_was_ready = self.llm.ready
            if self.tray:
                self.tray.update_menu()
        if time.time() - self.last_save > SAVE_EVERY_S:
            self._save()
        self._tick()
        now = time.monotonic()
        if changed:
            self._activity = now
        self._poll_ms = poll_delay(now - self._activity)
        self._poll_job = self.root.after(self._poll_ms, self._poll)

    def _save(self):
        self.predictor.user.decay(time.time())  # раз в неделю, если приложение не перезапускали
        try:
            if self.predictor.user.dirty:
                self.predictor.user.save(C.USER_MODEL)
            if self.stats.dirty:
                self.stats.save()
            self.usage.flush()
        except OSError:
            log.exception("не сохранил модель/статистику")
        self.last_save = time.time()

    def _learn(self, text: str):
        if self.cfg.learn and WORD_END_RE.search(text):
            got = self.predictor.learn_from(text)
            if got:
                self.learned = (self._end() - len(self.buf) + len(text), *got)

    def _unlearn_if_erased(self):
        """Курсор ушёл назад внутрь только что выученного слова — его исправляют: счёт откатываем."""
        if self.learned and self._end() < self.learned[0]:
            self.predictor.user.unlearn(*self.learned[1:])
            log.debug("исправленное слово разучено")
            self.learned = None

    def _handle(self, kind: str, *args) -> bool:
        """→ True, если событие могло изменить текст (тогда LLM спрашиваем заново)."""
        if kind != "layout_undo":
            self._disarm_undo()  # любое событие, кроме самой отмены, закрывает окно отмены автозамены
        if kind != "layout_key":
            self.manual = None
        if kind in ("enter", "reset", "dismiss", "layout_undo", "layout_key") and self.miss is not None:
            self.miss.other()
        if kind == "toggle":
            self._set_enabled(not self.shared.enabled)
            return True
        if kind == "variant":
            if len(self.variants) > 1:
                self.usage.cycle()
            self._cycle(args[0])
            return False
        hwnd = user32.GetForegroundWindow()
        if hwnd != self.fg:
            self.fg = hwnd
            self._reset_buf()
            self._hide()  # подсказка была для другого окна: по Tab в новом её вставлять нельзя
            self.need_check = True
            self.pending_reload = False
            self.sync_ok, self.sync_miss = True, 0
            self.exe = process_name(hwnd)
            self.blocked = self.exe in self.cfg.blacklist
            self.password = False
            self._update_gate()
            self.usage.reset("window")
            log.debug("окно: %s%s", self.exe, " (чёрный список)" if self.blocked else "")
        if not self.shared.enabled or self.blocked:
            self._pass_through(kind, args)  # хук проглотил клавишу раскладки, а мы здесь молчим
            if self.buf:
                self._reset_buf()
            self.usage.reset("off")
            return True
        self.sync_due = kind in ("char", "bs")  # после набора сверим буфер с полем (_sync_field)
        if kind in ("char", "bs", "enter", "reset"):
            self.partial = False
        if kind == "char":
            ch = args[0]
            if not self.llm.running:
                self._manage_llm()
            if self.need_check:
                self.need_check = False
                self.password = self.caret.is_password(hwnd)
                self._update_gate()
                if self.password:
                    log.debug("поле пароля — пропускаю")
                elif not self.buf:
                    # после клика/стрелок/смены окна текст перед курсором перечитаем из поля в _refresh:
                    # там приложение уже вставило символ и не надо гадать, есть ли он в поле
                    self.pending_reload = True
            if self.password:
                if self.buf:
                    self._reset_buf()
                self.usage.reset("password")
                return True
            now = time.time()
            if self.last_key and now - self.last_key < 1.5:
                self.gaps = (self.gaps + [(now - self.last_key) * 1000])[-6:]
            self.last_key = now
            self.stats.typed()
            self.usage.char(ch)
            self._miss_char(ch)
            if not re.match(rf"[{WORD_CH}\-'’]", ch):
                self._learn(self.buf)
            self._append(ch)
            self.dismissed = False
            self._layout_auto(ch)
        elif kind == "bs":
            self._miss_bs(self.buf[-1:])
            self.buf = self.buf[:-1]
            self._unlearn_if_erased()
            self.usage.bs()
            self.dismissed = False
        elif kind == "enter":
            # буфер не стираем: предыдущие абзацы и сообщения — контекст для LLM
            self._learn(self.buf)
            self.usage.char("\n")
            if self.buf:
                self._append("\n")
            self.need_check = True
            self.anchor = None
            self.dismissed = False
        elif kind == "reset":
            self.usage.reset(args[0] if args else "")
            self._reset_buf()
            self.need_check = True
            self.pending_reload = False
        elif kind == "dismiss":
            s = self.suggestion
            if s is not None and s.level == "layout":
                self.layout_floor = self._end()  # отклонённое исправление детектор не предлагает снова
                if s.fix is not None and self.layout is not None:
                    # слово, которое детектор считает опечаткой, не учится: без счёта отказов плашка была бы вечной
                    self._count_reject(s.fix, self._layout_info(s, s.fix.to_cyr))
            self.dismissed = True
            self.usage.dismiss()
        elif kind == "layout_undo":
            return self._layout_undo()
        elif kind == "layout_key":
            return self._layout_key(*args)
        elif kind in ("accept", "accept_word"):
            self._accept(kind == "accept_word", *args)
        return True

    # --- буфер: скользящее окно BUF_MAX с абсолютным смещением начала ---
    def _end(self) -> int:
        """Абсолютная позиция курсора: не сбивается, когда начало буфера обрезается."""
        return self.buf_off + len(self.buf)

    def _append(self, s: str):
        self.buf += s
        over = len(self.buf) - BUF_MAX
        if over > 0:
            self.buf = self.buf[over:]
            self.buf_off += over

    def _reset_buf(self, text: str = ""):
        """Другой текст вместо прежнего: старые ответы LLM, позиция каретки и показанные подсказки к нему не относятся."""
        self.buf_off += len(self.buf) + 1
        self.buf = text[-BUF_MAX:]
        self.anchor = None
        self.history.clear()

    def _accept(self, word: bool = False, vk: int = VK_TAB, sid: int | None = None):
        """Tab — вся подсказка; Ctrl+→ (word) — до конца следующего слова, остаток остаётся на плашке.

        sid — какую подсказку хук видел на плашке при нажатии: вставляем её, а не ту, что успела смениться.
        Нет её (окно сменилось, плашку уже спрятали) — клавишу хук проглотил зря: возвращаем её в окно.
        """
        s = self.history.get(sid) if sid is not None else self.suggestion
        if not s:
            send_key(vk, extended=vk == VK_RIGHT)
            return
        word = word and not s.replace  # замену слова (раскладка, сниппет) по кускам не принимаем
        chunk = accept_chunk(s.insert) if word else s.insert
        last = chunk == s.insert.rstrip()
        text = chunk + (" " if last and s.whole_word else "")
        self.overlay.hide()
        if s.level == "layout":
            cyr = s.fix.to_cyr if s.fix else bool(CYR_RE.search(text))
            info = self._layout_info(s, cyr)
            row = self.usage.layout_done("accepted", info, self.exe)
            self.usage.reset("layout")
            self.stats.layout_fixed(s.replace, len(text))
            # до вставки, как в _apply_layout: нажатия во время вставки хук переведёт уже в новой раскладке
            self._switch(cyr, row)
        self._insert(text, erase=s.replace)
        if s.replace:
            self.buf = self.buf[:max(0, len(self.buf) - s.replace)]
        if s.level != "layout":
            self.usage.accept(text)
        # учим только слова, после которых уже стоит пробел; недописанное (основа, кусок по Ctrl+→)
        # выучится, когда допечатаешь его и поставишь разделитель
        tmp = self.buf
        pieces = re.split(r"(\s+)", text)
        for i, piece in enumerate(pieces):
            tmp += piece
            if piece.strip() and i + 1 < len(pieces) and pieces[i + 1]:
                self._learn(tmp)
        self._append(text)
        self.suggestion = None
        saved = text if s.level == "layout" else text[s.replace:]  # сокращение напечатано руками
        self.stats.accepted(saved, llm=s.level in ("llm", "long"), long=s.level == "long",
                            new=not self.partial, word=word)
        self.partial = word and not last
        log.debug("принято (%s%s)", s.level, ", слово" if word else "")

    def _layout_info(self, s, cyr: bool) -> dict:
        """Форма показанного исправления для журнала. Прежнее правило (детектор ещё грузится) — слово в одно «слово»."""
        if s.fix is not None:
            how = "delim" if s.fix.closed else "inword"
            return layout_info(s.fix.original, s.fix.to_cyr, s.fix.words, self._layout_mode(), how)
        return layout_info("x" * s.replace, cyr, 1, self._layout_mode(), "inword")

    def _can_suggest(self) -> bool:
        return (self.shared.enabled and not self.blocked and not self.password
                and not self.dismissed and bool(self.buf.strip()))

    def _suggest(self):
        if not self._can_suggest():
            return None
        t0 = time.perf_counter()
        # длинная подсказка из паузы, пока печатаешь ровно её, важнее коротких
        s = self._long_suggestion()
        if not s:
            lt = self._layout_text()
            ng = self.predictor.suggest(self.buf, layout_text=lt)
            s = ng if ng and ng.level == "layout" else self._merge(ng, self._llm_suggestion())
        if s and s.level not in ("long", "layout", "snippet") and not self._worth_showing(s):
            s = None
        if DEBUG:
            log.debug("suggest %.1f мс → %r (%s)", (time.perf_counter() - t0) * 1000, s and s.insert, s and s.level)
        return s

    def _fast(self) -> bool:
        """Быстрый набор: медиана последних пауз меньше порога и печатаешь прямо сейчас."""
        if len(self.gaps) < 3 or time.time() - self.last_key > 1.0:
            return False
        return sorted(self.gaps)[len(self.gaps) // 2] < self.cfg.fast_typing_ms

    def _worth_showing(self, s) -> bool:
        """На быстром наборе подсказку обычно не успевают заметить: показываем только длинные и без следующего слова."""
        n = len(s.insert.strip())
        if n < self.cfg.min_insert_chars:
            return False
        if not self._fast():
            return True
        if s.level != "llm" and not parse_context(self.buf)[2]:
            return False  # следующее слово n-грамм без префикса — 73% мимо вживую
        return n >= self.cfg.fast_min_insert

    @staticmethod
    def _merge(ng, ls):
        if not ls:
            return ng
        if not ng:
            return ls
        if ls.insert.startswith(ng.insert):
            return ls  # согласны, LLM видит дальше
        if ng.insert.startswith(ls.insert):
            return ng
        return ls if ls.confidence > ng.confidence else ng

    def _llm_suggestion(self):
        """Ответ LLM, если пользователь печатает ровно то, что она предлагала."""
        r = self.llm_res
        if not r:
            return None
        typed = typed_after(self.buf, self.buf_off, r.text, r.off)
        if typed is None or not r.insert.startswith(typed):
            self.llm_res = None
            return None
        rest = r.insert[len(typed):]
        if len(rest.strip()) < 2:
            return None
        return Suggestion(insert=rest, word="", confidence=r.prob, level="llm", whole_word=True)

    # --- LLM ---
    def _manage_llm(self, refresh_power: bool = False):
        now = time.time()
        if refresh_power or now - self.power_checked > 15:
            self.on_ac = on_ac_power()
            self.power_checked = now
        cfg = self.cfg
        idle = now - self.last_key > cfg.llm_idle_unload_min * 60
        want = (cfg.llm_enabled and self.shared.enabled and self.llm.available
                and (self.on_ac or not cfg.llm_only_on_ac) and not idle)
        if want and not self.llm.running:
            self.llm.start()
        elif not want and self.llm.running:
            reason = ("батарея" if not self.on_ac else "простой" if idle else "выключена")
            self.llm.stop(reason)
            self.llm_res = None
            self.variants = []
        else:
            return
        if self.tray:
            self.tray.update_menu()

    def _ask_llm(self, seq: int):
        if seq != self.last_seq or not self.llm.ready or not self._can_suggest():
            return
        _c2, _c1, prefix, _start, ends_space = parse_context(self.buf)
        if not prefix and not ends_space:
            return
        off = self.buf_off
        cb = lambda r: self.cmds.put(lambda: self._on_llm(r, off))
        self.llm.request(seq, self.buf, prefix, self.cfg.llm_min_prob, self.cfg.llm_max_words, cb,
                         self._header())

    def _banned_in(self, r) -> bool:
        """В ответе LLM запрещённое слово (Словарь → «Запрещённые»): недописанное слово перед курсором + вставка."""
        if not self.predictor.banned:
            return False
        tail = re.search(r"\S*$", r.text).group(0)
        return any(w in self.predictor.banned for w in WORD_RE.findall((tail + r.insert).lower()))

    def _on_llm(self, r, off: int = 0):
        r.off = off
        if self._banned_in(r):
            return
        self.llm_res = r
        if DEBUG:
            log.debug("LLM %.0f мс → %r p=%.2f", self.llm.last_ms, r.insert, r.prob)
        if self._llm_suggestion():
            self._refresh(self.last_seq)

    def _header(self) -> str:
        return window_header(self.fg, self.exe) if self.cfg.llm_header and self.fg else ""

    # --- режим паузы ---
    def _ask_long(self, seq: int):
        if seq != self.last_seq or not self.llm.ready or not self._can_suggest():
            return
        cur = self._long_suggestion()
        if cur and len(cur.insert.split()) >= 3:
            return  # ещё есть что принимать — не перезапрашиваем
        _c2, _c1, prefix, _start, ends_space = parse_context(self.buf)
        if not prefix and not ends_space:
            return  # сразу после знака без пробела не гадаем
        if self.buf.endswith("\n") and not self.cfg.llm_after_enter:
            return
        if place(self.buf) == "sent" and not getattr(self.cfg, "llm_after_sentence", True):
            return
        off = self.buf_off
        cb = lambda r: self.cmds.put(lambda: self._on_long(r, off))
        self.llm.request_long(seq, self.buf, prefix, cb, self._header())

    def _on_long(self, r, off: int = 0):
        r.off = off
        if typed_after(self.buf, self.buf_off, r.text, r.off) is None:
            return  # текст уже другой
        if self.variants and (self.variants[0].off, self.variants[0].text) != (r.off, r.text):
            self.variants, self.vi = [], 0  # ответ на новый текст вытесняет старые варианты
        if self._banned_in(r):  # вариант дорос до запрещённого слова — убираем его
            cur = self.variants[self.vi] if self.vi < len(self.variants) else None
            self.variants = [v for v in self.variants if v.variant != r.variant]
            # показанный вариант остаётся на месте, даже если убрали стоящий перед ним
            self.vi = next((i for i, v in enumerate(self.variants) if v is cur), 0)
            self._refresh(self.last_seq)
            return
        for i, v in enumerate(self.variants):
            if v.variant == r.variant:
                self.variants[i] = r
                break
        else:
            self.variants.append(r)
        if r.variant > 0 and self.cfg.llm_rank:
            cur = self.variants[self.vi] if self.vi < len(self.variants) else None
            self.variants.sort(key=lambda v: -v.prob)
            self.vi = self.variants.index(cur) if self.vi and cur in self.variants else 0
        if DEBUG:
            log.debug("LLM пауза → [%s%s] %r (%.2f)", r.variant, "" if r.done else "…", r.insert, r.prob)
        self._refresh(self.last_seq)

    def _long_suggestion(self):
        """Показанный вариант из паузы, если пользователь печатает ровно его; несовпавшие варианты выбрасываются."""
        if not self.variants:
            self.shared.variants = 0
            return None
        def rest_of(v):  # что осталось допечатать из варианта; None — пишут уже не его
            t = typed_after(self.buf, self.buf_off, v.text, v.off)
            return v.insert[len(t):] if t is not None and v.insert.startswith(t) and len(v.insert) > len(t) else None

        cur = self.variants[min(self.vi, len(self.variants) - 1)]
        live = [v for v in self.variants if rest_of(v) is not None]
        if len(live) != len(self.variants):
            self.variants = live
            self.vi = live.index(cur) if cur in live else 0
        self.shared.variants = len(self.variants)
        if not self.variants:
            return None
        r = self.variants[self.vi]
        rest = rest_of(r)
        if len(rest.strip()) < 2:
            return None
        return Suggestion(insert=rest, word="", confidence=r.prob, level="long", whole_word=True)

    def _cycle(self, d: int):
        if len(self.variants) > 1:
            self.vi = (self.vi + d) % len(self.variants)

    def _install_llm(self):
        """Скачать llama-server и модель в фоне; прогресс — в строке статуса трея."""
        from . import setup_llm as S
        if self.llm_installing or self.llm.available:
            return
        self.llm_installing = "0%"
        self._notify(f"Скачиваю ИИ-модель, {S.download_size(self.cfg.llm_model) / 1e6:.0f} МБ")

        def progress(label, done, total):
            self.llm_installing = f"{label} {done * 100 // total}%"

        def work():
            try:
                S.install(self.cfg.llm_model, progress)
                self._notify("ИИ-модель установлена ✦")
            except Exception as e:  # сеть, диск, битый файл — сообщить и дать повторить
                log.exception("установка ИИ-модели")
                self._notify(f"Не удалось скачать ИИ-модель: {e}")
            finally:
                self.llm_installing = None
                self._refresh_tray()

        threading.Thread(target=work, daemon=True, name="llm-setup").start()

    def _llm_status(self) -> str:
        if self.llm_installing:
            return f"ИИ: загрузка, {self.llm_installing}"
        if not self.cfg.llm_enabled:
            return "LLM: выключена"
        if not self.llm.available:
            return "LLM: нет модели"
        if not self.llm.running and self.llm.fails:
            return "LLM: сервер падает, см. server.log"
        if self.llm.ready:
            return "LLM: работает ✦"
        if self.llm.running:
            return "LLM: загружается…"
        if self.cfg.llm_only_on_ac and not self.on_ac:
            return "LLM: ждёт зарядку"
        return "LLM: выгружена до набора"

    def _toggle_llm(self):
        self.cfg.llm_enabled = not self.cfg.llm_enabled
        self.cfg.save()
        self._manage_llm(refresh_power=True)
        self._refresh_tray()

    def _show(self, s, x, y, h, seq):
        long = s.level == "long"
        llm = long or s.level == "llm"
        hint = self.key_hint + (" ✦✦" if long else " ✦" if llm else " ⇄" if s.level == "layout"
                                else " ✎" if s.level in ("snippet", "fix") else "")  # ✎ — набранное заменится
        if long and len(self.variants) > 1:
            hint += f" {self.vi + 1}/{len(self.variants)}"
        self.overlay.show(s.insert, x, y, h, hint, long=long)
        if s != self.suggestion:
            self.shown_id += 1
            self.history[self.shown_id] = s
            self.history.pop(self.shown_id - 8, None)
        self.shared.shown_id = self.shown_id
        self.suggestion = s
        self.shared.variants = len(self.variants) if long else 0  # Alt+↓ — только когда видна ✦✦
        self.shared.shown_seq = seq
        self.shared.visible = True
        self.stats.shown(s.insert, llm=llm, long=long)
        if s.level == "layout":
            self.usage.layout_shown(self._layout_info(s, bool(CYR_RE.search(s.insert))), self.exe)
        else:
            self.usage.shown(self.buf, s.insert, s.level, self.exe, len(self.variants) if long else 0, conf=s.confidence)

    def _hide(self):
        self.overlay.hide()
        self.suggestion = None
        self.shared.visible = False

    def _show_now(self):
        """Сразу после нажатия: позицию прикидываем по последней точной + ширина символов."""
        s = self._suggest()
        if not s:
            self._hide()
            return
        if self.anchor:
            x, y, h, n0 = self.anchor
            n = self._end() - n0
            if 0 <= n <= 40:
                self._show(s, int(x + n * self.cw_ratio * h), y, h, self.last_seq)
                return
        self._hide()  # позиции нет — ждём точную

    def _refresh(self, seq: int):
        if seq != self.last_seq:
            return  # пришли новые нажатия — посчитаем по ним
        if self.sync_due or self.pending_reload:
            self._sync_field()
        s = self._suggest()
        if not s:
            self._hide()
            return
        pos = self.caret.locate(self.fg)
        if not pos:
            log.debug("каретка не найдена (%s)", self.exe)
            self.stats.no_caret(self.exe or "?")
            self._hide()
            return
        x, y, h, src = pos
        if self.anchor:
            ax, ay, ah, an = self.anchor
            dn = self._end() - an
            if abs(y - ay) < 3 and ah == h and 0 < dn <= 40:
                r = (x - ax) / dn / h
                if 0.2 < r < 1.2:
                    self.cw_ratio = 0.7 * self.cw_ratio + 0.3 * r
        self.anchor = (x, y, h, self._end())
        if DEBUG:
            log.debug("каретка %s: %s,%s h=%s", src, x, y, h)
        self._show(s, x, y, h, seq)

    def _sync_field(self):
        """Сверить буфер с текстом поля перед курсором (UIA), когда приложение уже обработало нажатие.

        Хук переводит клавиши в буквы по раскладке в момент нажатия; сразу после Alt+Shift она бывает ещё
        старой — в буфер попадало «ащк» вместо «for», и LLM продолжала «ш шт кфтпу». Поле — источник правды.
        """
        reload, self.pending_reload = self.pending_reload, False
        self.sync_due = False
        if not self.sync_ok or self.password or self.blocked or not self.shared.enabled:
            return
        field = self.caret.text_before_caret(self.cfg.context_read_chars if reload else SYNC_CHARS)
        if not field:
            return
        if reload:
            # контекст после клика/смены окна; набранное после сброса, которого в поле ещё нет, — дописываем
            typed = self.buf
            lag = next((k for k in range(min(SYNC_LAG, len(typed)) + 1)
                        if field.endswith(typed[:len(typed) - k])), 0)
            self._reset_buf(field + (typed[len(typed) - lag:] if lag else ""))
            self.usage.reset("reload")
            log.debug("контекст из поля: %s симв.", len(field))
            return
        if field_agrees(self.buf, field):
            self.sync_miss = 0
            return
        self.sync_miss += 1
        if self.sync_miss >= SYNC_GIVE_UP:
            self.sync_ok = False
            log.info("поле в %s не совпадает с набором — сверку в этом окне выключаю", self.exe)
            return
        full = self.caret.text_before_caret(self.cfg.context_read_chars) or field
        log.debug("буфер разошёлся с полем — перечитал %s симв.", len(full))
        self._reset_buf(full)
        self.usage.reset("resync")

    def _insert(self, text: str, erase: int = 0):
        """Вставка по символу; нажатия за это время хук откладывает и повторяет после, а не вперемешку с текстом.

        erase — сначала стереть столько символов (Backspace с меткой MAGIC: хук их не считает набором).
        """
        self.shared.inserting = self.shared.typing = True
        try:
            for _ in range(erase):
                send_key(VK_BACK, extra=MAGIC)
            send_text(text)
        finally:
            self.shared.typing = False
            self.hook.release_held()

    # --- управление ---
    def _set_enabled(self, on: bool):
        self.shared.enabled = on
        self.cfg.enabled = on
        self.cfg.save()
        self._reset_buf()
        self._hide()
        log.info("подсказки %s", "включены" if on else "выключены")
        self._refresh_tray()

    # --- обучение: считает дочерний процесс, здесь только запуск и подмена модели ---
    def _retrain(self) -> bool:
        if not self.trainer.start("manual"):
            log.info("переобучение уже идёт")
            return False
        return True

    def _on_train(self, ev: dict):
        """Событие Trainer (через cmds, главный поток)."""
        t = ev["type"]
        if t != "stage":
            log.info("обучение: %s", ev)
        if t == "stage":
            if ev["stage"] == "start":
                self._notify("Переобучение началось")
        elif t == "done":
            if ev["accepted"]:
                self._reload_base()
                self._notify(self._done_text(ev))
            else:
                self._notify(f"новая модель хуже: {ev['metric']:.1f} против {ev['prev_metric']:.1f}% — оставил прежнюю")
        elif t == "error":
            self._notify("Переобучение не удалось: " + ev["error"][:120])
        elif t == "cancelled":
            self._notify("Переобучение отменено")
        if t != "stage":
            self._refresh_tray()

    @staticmethod
    def _done_text(ev: dict) -> str:
        text = f"готово за {ev['secs']:.0f} с: {ev['words']:,} слов".replace(",", " ")
        if ev["metric"] is None:
            return text
        if ev["prev_metric"] is None:
            return f"{text}, экономия {ev['metric']:.1f}%"
        return f"{text}, экономия {ev['prev_metric']:.1f} → {ev['metric']:.1f}%"

    def _reload_base(self):
        """Прочитать base_model.pkl в потоке (pickle держит GIL, на главном потоке тормозил бы хук) и подменить."""
        self._base_seq += 1
        seq = self._base_seq

        def work():
            try:
                base = BaseModel.load(C.BASE_MODEL)
                warm = Predictor(base, UserModel())  # прогрев самого дорогого запроса — тоже не на главном потоке
            except Exception:
                log.exception("не загрузил базовую модель")
                return
            self.cmds.put(lambda: self._set_base(base, warm._uni_cache, seq))

        threading.Thread(target=work, daemon=True, name="base-load").start()

    def _set_base(self, base: BaseModel, uni_cache: dict, seq: int):
        if seq != self._base_seq:
            return  # успела начаться более новая загрузка
        self.predictor.base = base
        self.predictor._uni_cache = uni_cache
        if self.layout is not None:
            self.layout._cache.clear()  # оценки слов зависят от счётов базовой модели
        log.info("базовая модель подменена: %s слов", f"{len(base.uni):,}")

    def _train_apply(self) -> bool:
        """Применить отложенную модель (её отклонили ворота)."""
        if not self.trainer.apply_pending():
            return False
        self._reload_base()
        self._notify("Отложенная модель применена")
        self._refresh_tray()
        return True

    def _train_rollback(self) -> bool:
        if not self.trainer.rollback():
            return False
        self._reload_base()
        self._notify("Вернул предыдущую модель")
        self._refresh_tray()
        return True

    def _auto_train(self, now: float):
        """Раз в сутки на зарядке и в простое, если корпус изменился (проверка — не чаще раза в 5 мин)."""
        self._train_check = now
        if (not self.cfg.train_auto or self.trainer.state["running"] or not self.on_ac
                or now - self.last_key < 600 or now - self.trainer.last_run_ts() < 86400):
            return

        def work():  # обход заметок и экспорта — не на главном потоке
            try:
                if self.trainer.corpus_changed():
                    self.cmds.put(lambda: self.trainer.start("auto"))
            except Exception:
                log.exception("проверка корпуса")

        threading.Thread(target=work, daemon=True, name="corpus-check").start()

    # --- трей ---
    STAGE_NAMES = {"start": "запуск", "notes": "Тексты", "telegram": "Telegram",
                   "claude": "Claude", "count": "Подсчёт слов", "prune": "Отсев", "write": "Запись",
                   "check": "Проверка"}

    def _tray_state(self) -> tuple:
        if not self.shared.enabled:
            base = "off"
        elif self.typed_exe and self.typed_exe in self.cfg.blacklist:
            base = "blocked"
        else:
            base = "on"
        if self.trainer.state["running"]:
            dot = "train"
        elif self.llm.running and not self.llm.ready:
            dot = "llm"
        else:
            dot = None
        return base, dot

    def _menu_state(self) -> tuple:
        """Всё, из чего собраны подписи меню: pystray на Windows перестраивает меню только по update_menu()."""
        return (self._state_line(), self._llm_status(),
                self.trainer.state["running"] and self._train_status(),
                self.typed_exe, self.llm.available, self.llm_installing, self.cfg.llm_enabled)

    def _refresh_tray(self):
        """Иконку, подсказку и меню трогаем только при смене состояния."""
        if not self.tray or not self._icons:
            return
        key, menu = self._tray_state(), self._menu_state()
        if key == self._icon_state and menu == self._menu_key:
            return
        self._menu_key = menu
        try:
            if key != self._icon_state:
                self._icon_state = key
                self.tray.icon = self._icons[key]
                self.tray.title = self._tray_title(*key)
            self.tray.update_menu()
        except Exception:
            log.exception("не обновил трей")

    def _tray_title(self, base: str, dot: str | None) -> str:
        title = "Halfword: подсказки при наборе"
        if self.pause_until:
            title += f" (пауза до {time.strftime('%H:%M', time.localtime(self.pause_until))})"
        elif base == "off":
            title += " (выключено)"
        elif base == "blocked":
            title += f" (выключено в {self.typed_exe})"
        if dot == "train":
            title += ", идёт переобучение"
        elif dot == "llm":
            title += ", загружается ИИ"
        return title

    def _notify(self, text: str):
        if not self.tray:
            return
        try:
            self.tray.notify(text, "Halfword")
        except Exception:
            log.exception("не показал уведомление")

    def _tick(self):
        """Из _poll раз в секунду: конец паузы, падение LLM-сервера, иконка, автообучение."""
        now = time.time()
        if now - self._tick_at < 1.0:
            return
        self._tick_at = now
        if self.pause_until:
            if self.shared.enabled:     # включили вручную посреди паузы
                self.pause_until = 0.0
                self._icon_state = None
            elif now >= self.pause_until:
                self._end_pause()
        if self.llm.fails == 0:
            self._llm_warned = False
        elif not self.llm.running and not self._llm_warned:
            self._llm_warned = True
            self._notify("LLM-сервер не запускается, см. server.log")
        self._refresh_tray()
        if self.pair is not None and not self.pair.ok and now - self._pair_check > 30:
            self._pair_check = now  # поставили русскую или английскую раскладку — подхватываем без перезапуска
            if layout_pair().ok:
                self._init_layout()
        if now - self._train_check > 300:
            self._auto_train(now)

    def _toggle_pause(self):
        """Пауза на 1 час: подсказки выключены, config.enabled не трогаем; повторный клик снимает."""
        if self.pause_until:
            self._end_pause()
            return
        self.pause_until = time.time() + 3600
        self.shared.enabled = False
        self._reset_buf()
        self._hide()
        self._icon_state = None
        log.info("пауза до %s", time.strftime("%H:%M", time.localtime(self.pause_until)))
        self._refresh_tray()

    def _end_pause(self):
        self.pause_until = 0.0
        self.shared.enabled = self.cfg.enabled
        self._icon_state = None
        log.info("пауза закончена")
        self._refresh_tray()

    def _toggle_exe(self):
        """«Выключить/Включить в <exe>» для программы, где печатали последний раз."""
        exe = self.typed_exe
        if not exe:
            return
        bl = self.cfg.blacklist
        if exe in bl:
            bl[:] = [x for x in bl if x != exe]
        else:
            bl.append(exe)
        self.cfg.save()
        self.blocked = self.exe in bl
        if self.blocked:
            self._reset_buf()
            self._hide()
        log.info("%s: %s", exe, "в чёрном списке" if exe in bl else "убрана из чёрного списка")
        self._icon_state = None
        self._refresh_tray()

    def _train_status(self) -> str:
        st = self.trainer.state
        name = self.STAGE_NAMES.get(st["stage"], st["label"])
        return f"Переобучение: {name}, {st['pct']}%…"

    def _state_line(self) -> str:
        """Первая строка меню: что сейчас с подсказками."""
        if self.pause_until:
            return "Пауза до " + time.strftime("%H:%M", time.localtime(self.pause_until))
        if not self.shared.enabled:
            return "Подсказки выключены"
        if self.typed_exe and self.typed_exe in self.cfg.blacklist:
            return f"Выключено в {self.typed_exe}"
        return "Подсказки работают"

    def _start_tray(self):
        try:
            import pystray
            from . import icon
        except ImportError:
            log.warning("pystray/Pillow не установлены — без иконки в трее")
            return
        self._icons = icon.all_states()
        M, SEP = pystray.MenuItem, pystray.Menu.SEPARATOR
        put = lambda f: (lambda: self.cmds.put(f))
        running = lambda _: self.trainer.state["running"]
        menu = pystray.Menu(
            # клик по иконке — панель; пункт по умолчанию показан жирным
            M("Открыть панель", lambda: self._open_panel(), default=True, enabled=lambda _: self.panel is not None),
            # строки состояния — серые, без действия
            M(lambda _: self._state_line(), None, enabled=False),
            M(lambda _: self._llm_status(), None, enabled=False),
            M(lambda _: self._train_status(), None, enabled=False, visible=running),
            SEP,
            M("Подсказки", put(lambda: self._set_enabled(not self.shared.enabled)),
              checked=lambda _: self.shared.enabled and not self.pause_until),
            M(lambda _: "Снять паузу" if self.pause_until else "Пауза на 1 час", put(self._toggle_pause)),
            M(lambda _: ("Включить в " if self.typed_exe in self.cfg.blacklist else "Выключить в ") + self.typed_exe,
              put(self._toggle_exe), visible=lambda _: bool(self.typed_exe)),
            SEP,
            M("ИИ-подсказки ✦", put(self._toggle_llm), checked=lambda _: self.cfg.llm_enabled,
              visible=lambda _: self.llm.available),
            M("Установить ИИ-модель…", put(self._install_llm),
              visible=lambda _: not self.llm.available and not self.llm_installing),
            M("Переобучить сейчас", put(self._retrain), visible=lambda _: not self.trainer.state["running"]),
            M("Отменить переобучение", lambda: self.trainer.cancel(), visible=running),
            SEP,
            M("Папка данных", lambda: os.startfile(C.DATA_DIR)),
            M("Выход", put(lambda: self.root.quit())),
        )
        self._icon_state = self._tray_state()
        self.tray = pystray.Icon("halfword", self._icons[self._icon_state], "Halfword: подсказки при наборе", menu)
        self.tray.run_detached()
        if self.cfg.llm_enabled and not self.llm.available:
            self._notify("ИИ-подсказки ✦ не установлены: трей → «Установить ИИ-модель…»")

    def _open_panel(self, tab: str = ""):
        """Панель в браузере; tab — вкладка (overview, usage, train, settings…)."""
        if self.panel:
            webbrowser.open(self.panel.url + ("#" + tab if tab else ""))

    def _shutdown(self):
        if self.panel:
            self.panel.stop()
        self.trainer.cancel()  # дочерний процесс обучения не должен пережить приложение
        self.hook.stop()
        self.llm.stop("выход")
        self.usage.reset("exit")
        self._save()
        if self.tray:
            self.tray.stop()
        log.info("остановлено")


def main():
    setup_logging()
    if not single_instance():
        log.info("уже запущено")
        return
    set_dpi_aware()
    try:
        App().run()
    except Exception:
        log.exception("упало")  # под pythonw иначе ошибку не увидеть
        raise
