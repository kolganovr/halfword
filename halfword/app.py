"""Склейка: хук → буфер текста → предсказатель → плашка у каретки; иконка в трее."""
from __future__ import annotations

import ctypes
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
from .caret import CaretLocator
from .llm import LLM, on_ac_power
from .model import DOC_WEIGHT, WORD_CH, BaseModel, Predictor, Suggestion, UserModel, parse_context
from .overlay import Overlay
from .stats import Stats
from .trainer import Trainer
from .usage import Usage
from .winhook import (MAGIC, VK_BACK, VK_RIGHT, VK_TAB, Hook, Shared, process_name, send_key, send_text,
                      switch_layout, user32, window_header)

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

    def __init__(self):
        self.cfg = C.Config.load()
        self.cmds: queue.Queue = queue.Queue()  # команды из трея и потоков
        self.trainer = Trainer(lambda ev: self.cmds.put(lambda: self._on_train(ev)))
        self._base_seq = 0        # какая загрузка базовой модели последняя (старые ответы выбрасываем)
        self.predictor = Predictor(self._load_base(), UserModel.load(C.USER_MODEL))
        self._apply_predictor_cfg()
        self.predictor.user.decay(time.time())  # набранное руками тает: полураспад год
        self.stats = Stats(C.STATS_FILE)
        self.usage = Usage(C.USAGE_FILE)
        self.events: queue.Queue = queue.Queue()
        self.shared = Shared(enabled=self.cfg.enabled, accept_right=self.cfg.accept_key == "right")
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
        self.stats_win = None
        # трей
        self.pause_until = 0.0    # подсказки на паузе до этого времени (в config.enabled не пишется)
        self.typed_exe = ""       # последняя программа, где печатали (self.exe прыгает на трей при клике)
        self._tick_at = 0.0
        self._train_check = 0.0
        self._llm_warned = False
        self._icons: dict = {}
        self._icon_state = None

    def _apply_predictor_cfg(self):
        """Пороги и переключатели предсказателя из config (при запуске и после правки в панели)."""
        cfg, p = self.cfg, self.predictor
        for k in C.PREDICTOR_KEYS:
            setattr(p, k, getattr(cfg, k))
        p.min_insert = cfg.min_insert_chars
        p.next_min_tri = cfg.next_min_tri
        p.doc_weight = DOC_WEIGHT if cfg.doc_cache else 0.0
        p.layout_fix = cfg.layout_fix
        p.snippets = {k.lower(): v for k, v in cfg.snippets.items()}
        p.banned = {w.lower() for w in cfg.banned_words}

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
        self.root.after(10, self._poll)
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    def _poll(self):
        changed = typed = False
        try:
            while True:
                ev = self.events.get_nowait()
                self.last_seq = ev[0]
                typed = self._handle(ev[1], *ev[2:]) or typed
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
        self.root.after(8, self._poll)

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
            self.usage.reset("window")
            log.debug("окно: %s%s", self.exe, " (чёрный список)" if self.blocked else "")
        if not self.shared.enabled or self.blocked:
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
            if not re.match(rf"[{WORD_CH}\-'’]", ch):
                self._learn(self.buf)
            self._append(ch)
            self.dismissed = False
        elif kind == "bs":
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
            self.dismissed = True
            self.usage.dismiss()
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
        self._insert(text, erase=s.replace)
        if s.replace:
            self.buf = self.buf[:max(0, len(self.buf) - s.replace)]
            if s.level == "layout":  # дальше печатать в нужной раскладке
                switch_layout(self.fg, bool(CYR_RE.search(text)))
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

    def _can_suggest(self) -> bool:
        return (self.shared.enabled and not self.blocked and not self.password
                and not self.dismissed and bool(self.buf.strip()))

    def _suggest(self):
        if not self._can_suggest():
            return None
        t0 = time.perf_counter()
        # длинная подсказка из паузы, пока печатаешь ровно её, важнее коротких
        s = self._long_suggestion() or self._merge(self.predictor.suggest(self.buf), self._llm_suggestion())
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

    def _on_llm(self, r, off: int = 0):
        r.off = off
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
        off = self.buf_off
        cb = lambda r: self.cmds.put(lambda: self._on_long(r, off))
        self.llm.request_long(seq, self.buf, prefix, cb, self._header())

    def _on_long(self, r, off: int = 0):
        r.off = off
        if typed_after(self.buf, self.buf_off, r.text, r.off) is None:
            return  # текст уже другой
        if self.variants and (self.variants[0].off, self.variants[0].text) != (r.off, r.text):
            self.variants, self.vi = [], 0  # ответ на новый текст вытесняет старые варианты
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
        if self.tray:
            self.tray.update_menu()

    def _show(self, s, x, y, h, seq):
        long = s.level == "long"
        llm = long or s.level == "llm"
        hint = self.key_hint + (" ✦✦" if long else " ✦" if llm else " ⇄" if s.level == "layout"
                                else " ✎" if s.level == "snippet" else "")
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
        self.usage.shown(self.buf, s.insert, s.level, self.exe, len(self.variants) if long else 0)

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
        self.shared.inserting = True
        try:
            for _ in range(erase):
                send_key(VK_BACK, extra=MAGIC)
            send_text(text)
        finally:
            self.hook.release_held()

    # --- управление ---
    def _set_enabled(self, on: bool):
        self.shared.enabled = on
        self.cfg.enabled = on
        self.cfg.save()
        self._reset_buf()
        self._hide()
        log.info("подсказки %s", "включены" if on else "выключены")
        if self.tray:
            self.tray.update_menu()

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

    def _show_stats(self):
        self._text_window("Halfword — статистика", self.stats.report(), ("Segoe UI", 11))

    def _show_usage(self):
        from . import usage
        self.usage.flush()
        text = usage.report(usage.load(C.USAGE_FILE, 7)) + "\n\n(за 7 дней; за всё время — python -m halfword usage)"
        self._text_window("Halfword — журнал подсказок", text, ("Consolas", 10))

    def _text_window(self, title: str, text: str, font):
        if self.stats_win is not None and self.stats_win.winfo_exists():
            self.stats_win.destroy()
        w = tk.Toplevel(self.root)
        w.title(title)
        w.attributes("-topmost", True)
        w.resizable(False, False)
        tk.Label(w, text=text, justify="left", font=font, padx=16, pady=12).pack()
        tk.Button(w, text="Закрыть", command=w.destroy, padx=12).pack(pady=(0, 12))
        self.stats_win = w

    # --- трей ---
    STAGE_NAMES = {"start": "запуск", "notes": "Тексты", "telegram": "Telegram",
                   "claude": "Claude", "count": "Подсчёт слов", "prune": "Отсев", "write": "Запись",
                   "check": "Проверка"}

    def _make_icons(self):
        """Иконки по состоянию заранее: основа (вкл/выкл/программа в чёрном списке) × точка (обучение/LLM грузится)."""
        from PIL import Image, ImageDraw
        base_col = {"on": (40, 90, 200, 255), "off": (130, 130, 130, 255), "blocked": (130, 130, 130, 255)}
        dot_col = {None: None, "train": (245, 200, 40, 255), "llm": (90, 200, 240, 255)}
        for b, bc in base_col.items():
            for dot, dc in dot_col.items():
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                d.rounded_rectangle((4, 4, 60, 60), 12, fill=bc)
                d.text((19, 6), "T", fill="white", font_size=44)
                if b == "blocked":
                    d.line((10, 54, 54, 10), fill=(240, 140, 30, 255), width=7)
                if dc:
                    d.ellipse((36, 36, 62, 62), fill=dc, outline="white", width=3)
                self._icons[(b, dot)] = img

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

    def _refresh_tray(self):
        """Иконку и подсказку меняем только при смене состояния."""
        if not self.tray or not self._icons:
            return
        key = self._tray_state()
        if key == self._icon_state:
            return
        self._icon_state = key
        base, dot = key
        title = "Halfword: подсказки при наборе"
        if self.pause_until:
            title += f" (пауза до {time.strftime('%H:%M', time.localtime(self.pause_until))})"
        elif base == "off":
            title += " (выключено)"
        elif base == "blocked":
            title += f" (выключено в {self.typed_exe})"
        if dot == "train":
            title += ", идёт переобучение"
        try:
            self.tray.icon = self._icons[key]
            self.tray.title = title
            self.tray.update_menu()
        except Exception:
            log.exception("не обновил иконку трея")

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

    @staticmethod
    def _has_prev_model() -> bool:
        return C.BASE_MODEL.with_name(C.BASE_MODEL.stem + ".prev" + C.BASE_MODEL.suffix).exists()

    def _start_tray(self):
        try:
            import pystray
            import PIL  # noqa: F401
        except ImportError:
            log.warning("pystray не установлен — без иконки в трее")
            return
        self._make_icons()
        M = pystray.MenuItem
        running = lambda _: self.trainer.state["running"]
        menu = pystray.Menu(
            M("Подсказки включены", lambda: self.cmds.put(lambda: self._set_enabled(not self.shared.enabled)),
              checked=lambda _: self.shared.enabled),
            M(lambda _: ("Пауза до " + time.strftime("%H:%M", time.localtime(self.pause_until)))
              if self.pause_until else "Пауза на 1 час", lambda: self.cmds.put(self._toggle_pause)),
            M(lambda _: ("Включить в " if self.typed_exe in self.cfg.blacklist else "Выключить в ") + self.typed_exe,
              lambda: self.cmds.put(self._toggle_exe), visible=lambda _: bool(self.typed_exe)),
            M("Открыть панель…", self._open_panel, default=self.panel is not None,
              visible=lambda _: self.panel is not None),
            M("Статистика…", lambda: self.cmds.put(self._show_stats), default=self.panel is None),
            M("Журнал подсказок…", lambda: self.cmds.put(self._show_usage)),
            M(lambda _: self._llm_status(), None, enabled=False),
            M("Установить ИИ-модель…", lambda: self.cmds.put(self._install_llm),
              visible=lambda _: not self.llm.available and not self.llm_installing),
            M("LLM на зарядке", lambda: self.cmds.put(self._toggle_llm),
              checked=lambda _: self.cfg.llm_enabled),
            M(lambda _: self._train_status(), None, enabled=False, visible=running),
            M("Переобучить", lambda: self.cmds.put(self._retrain),
              enabled=lambda _: not self.trainer.state["running"]),
            M("Отменить переобучение", lambda: self.trainer.cancel(), visible=running),
            M("Применить отложенную модель", lambda: self.cmds.put(self._train_apply),
              visible=lambda _: self.trainer.state["pending"] and not self.trainer.state["running"]),
            M("Откатить модель", lambda: self.cmds.put(self._train_rollback),
              visible=lambda _: self._has_prev_model() and not self.trainer.state["running"]),
            M("Открыть папку данных", lambda: os.startfile(C.DATA_DIR)),
            M("Выход", lambda: self.cmds.put(self.root.quit)),
        )
        self._icon_state = self._tray_state()
        self.tray = pystray.Icon("halfword", self._icons[self._icon_state], "Halfword: подсказки при наборе", menu)
        self.tray.run_detached()
        if self.cfg.llm_enabled and not self.llm.available:
            self._notify("ИИ-подсказки ✦ не установлены: трей → «Установить ИИ-модель…»")

    def _open_panel(self):
        if self.panel:
            webbrowser.open(self.panel.url)

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
