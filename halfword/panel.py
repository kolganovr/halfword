"""Веб-панель управления на localhost: настройки, статистика, обучение, словарь, песочница.

Сервер живёт в daemon-потоке и сам ни tk, ни состояния App не трогает: всё идёт через call_main
(команда кладётся в app.cmds, главный цикл выполняет её в течение ~8 мс). Тексты набора наружу не отдаются —
только агрегаты и то, что пользователь сам ввёл в Песочницу.

Защита: слушаем только 127.0.0.1; токен на запуск (cookie st_token после GET /?t=…, для POST ещё и заголовок
X-Token); проверка Host (против DNS rebinding) и Origin (против CSRF).
"""
from __future__ import annotations

import copy
import ctypes
import hmac
import json
import logging
import math
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config as C
from .model import WORD_RE, parse_context

log = logging.getLogger("halfword.panel")

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC = {"/app.js": "application/javascript; charset=utf-8", "/style.css": "text/css; charset=utf-8"}
MAX_BODY = 64 * 1024
PORT_TRIES = 8          # panel_port, +1 … +7
CALL_TIMEOUT = 2.0
USAGE_TTL_S = 10.0      # журнал читаем с диска не чаще
SANDBOX_MAX = 2000


class PanelError(Exception):
    """Ошибка запроса с HTTP-кодом: сообщение уйдёт пользователю."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# --- вызов на главном потоке ---
def call_main(app, fn, timeout: float = CALL_TIMEOUT):
    """Выполнить fn() в главном цикле App и вернуть результат; исключение fn пробрасывается, нет ответа — TimeoutError.

    Опоздавшую команду главный цикл уже не выполнит: запрос ответил 503, побочных эффектов быть не должно.
    """
    done = threading.Event()
    box: dict = {}

    def run():
        if box.get("cancel"):
            return
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 — отдаём вызывающему потоку
            box["e"] = e
        finally:
            done.set()

    app.cmds.put(run)
    if not done.wait(timeout):
        box["cancel"] = True
        raise TimeoutError("главный цикл не ответил")
    if "e" in box:
        raise box["e"]
    return box.get("v")


# --- настройки: белый список ---
def _s(key, group, label, hint, type_, **kw):
    return dict(key=key, group=group, label=label, hint=hint, type=type_, **kw)


MORE = "Ниже — подсказки чаще, но больше мимо; выше — реже и точнее."
SETTINGS = [
    _s("min_conf_word", "Пороги n-грамм", "Уверенность: дописать слово", MORE, "float", min=0.05, max=0.95, step=0.01),
    _s("min_conf_next", "Пороги n-грамм", "Уверенность: следующее слово после пробела", MORE, "float", min=0.05, max=0.95, step=0.01),
    _s("min_support", "Пороги n-грамм", "Минимум встреч пары/тройки слов",
       "Меньше — чаще, но на случайных парах; больше — только устойчивые фразы.", "int", min=1, max=20),
    _s("short_penalty", "Пороги n-грамм", "Надбавка к порогу за короткий префикс",
       "За каждый символ короче 4 порог растёт на столько: больше — меньше подсказок на 1–2 буквах.", "float", min=0.0, max=0.3, step=0.01),
    _s("min_insert_chars", "Точность", "Минимальная длина подсказки (символов)",
       "Короче не показываем: Tab ради 1–2 букв не жмут. Меньше — чаще, больше — точнее.", "int", min=1, max=10),
    _s("fast_typing_ms", "Точность", "«Быстрый набор»: средняя пауза, мс",
       "Если пауза между нажатиями меньше — набор считается быстрым и подсказки строже.", "int", min=30, max=400),
    _s("fast_min_insert", "Точность", "Минимальная длина подсказки на быстром наборе",
       "Больше — меньше помех при беглом наборе, но реже подсказки.", "int", min=1, max=15),
    _s("doc_cache", "Точность", "Слова из текущего поля", "Слова и пары из того, что уже написано в поле, весят больше.", "bool"),
    _s("layout_fix", "Точность", "Исправлять раскладку", "«ghbdtn» → «привет»: подсказка при не той раскладке.", "bool"),
    _s("learn", "Общее", "Дообучаться на наборе", "Запоминать слова, набранные руками (кроме паролей и чёрного списка).", "bool"),
    _s("accept_key", "Общее", "Клавиша принятия", "Tab или стрелка вправо.", "enum", choices=["tab", "right"]),
    _s("overlay_theme", "Общее", "Тема плашки", "auto — как в Windows.", "enum", choices=["auto", "light", "dark"]),
    _s("llm_enabled", "LLM ✦", "LLM включена", "Языковая модель дописывает слова и фразы.", "bool"),
    _s("llm_only_on_ac", "LLM ✦", "Только на зарядке", "На батарее сервер модели выгружается.", "bool"),
    _s("llm_min_prob", "LLM ✦", "Порог вероятности LLM", MORE.replace("n-грамм", "LLM"), "float", min=0.1, max=0.95, step=0.01),
    _s("llm_max_words", "LLM ✦", "Слов в короткой подсказке", "Больше слов — длиннее, но чаще мимо.", "int", min=1, max=10),
    _s("llm_delay_ms", "LLM ✦", "Пауза до запроса, мс", "Меньше — быстрее ответ, но больше лишних запросов.", "int", min=0, max=1000),
    _s("llm_pause_ms", "LLM ✦", "Пауза до длинной подсказки ✦✦, мс", "Через сколько после остановки печати предлагать продолжение.", "int", min=200, max=3000),
    _s("llm_long_words", "LLM ✦", "Слов в длинной подсказке ✦✦", "Максимум слов продолжения в паузе.", "int", min=3, max=40),
    _s("llm_variants", "LLM ✦", "Вариантов длинной подсказки", "Переключаются Alt+↓/↑; больше — дольше считается.", "int", min=1, max=5),
    _s("llm_after_enter", "LLM ✦", "Подсказывать с новой строки", "Длинная подсказка в начале строки после Enter.", "bool"),
    _s("llm_header", "LLM ✦", "Передавать заголовок окна", "Название программы и окна первой строкой промпта: точнее, но модель видит заголовок.", "bool"),
    _s("text_dirs", "Обучение", "Папки с вашими текстами",
       "По одной на строку: заметки, документы, черновики — файлы .md и .txt, вложенные папки тоже. "
       "Применится при следующем обучении.", "paths"),
    _s("train_auto", "Обучение", "Переобучать автоматически", "Раз в сутки на зарядке и в простое, если корпус изменился.", "bool"),
    _s("train_gate_pp", "Обучение", "Допуск потери экономии, п.п.",
       "Новая модель хуже прежней больше чем на столько — не подменяет её.", "float", min=0.0, max=5.0, step=0.1),
    _s("tg_weight", "Обучение", "Вес Telegram", "Множитель веса ваших сообщений. Применится при следующем обучении.", "float", min=0.0, max=20.0, step=0.5),
    _s("claude_weight", "Обучение", "Вес сообщений Claude Code", "0 — не брать. Применится при следующем обучении.", "float", min=0.0, max=20.0, step=0.5),
]
SETTINGS_BY_KEY = {s["key"]: s for s in SETTINGS}
# какие ключи cfg уходят в App.llm.configure (имена — как в App.__init__)
LLM_CONFIGURE = {"llm_prompt_chars": "keep_long", "llm_long_tokens": "long_tokens", "llm_long_words": "long_words",
                 "llm_long_min_tok_p": "long_min_tok_p", "llm_variants": "variants"}


def _coerce(spec: dict, v):
    t = spec["type"]
    if t == "paths":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError("нужен список папок")
        out = [x.strip().strip('"') for x in v if x.strip()]
        missing = [x for x in out if not Path(os.path.expandvars(x)).expanduser().is_dir()]
        if missing:
            raise ValueError("нет такой папки: " + missing[0])
        return out
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError("нужно true/false")
        return v
    if t == "enum":
        if v not in spec["choices"]:
            raise ValueError("допустимо: " + ", ".join(spec["choices"]))
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("нужно число")
    if t == "int":
        if isinstance(v, float) and not v.is_integer():
            raise ValueError("нужно целое число")
        v = int(v)
    else:
        v = float(v)
        if not math.isfinite(v):
            raise ValueError("нужно число")
        v = round(v, 4)
    if not spec["min"] <= v <= spec["max"]:
        raise ValueError(f"допустимо от {spec['min']} до {spec['max']}")
    return v


def apply_config(app, changes: dict) -> dict:
    """Проверить и применить настройки. ВЫЗЫВАТЬ НА ГЛАВНОМ ПОТОКЕ (через call_main).

    Верные ключи применяются, неверные возвращаются в errors; → {"applied": {…}, "errors": {…}, "restart": […]}.
    """
    applied, errors = {}, {}
    for k, v in changes.items():
        spec = SETTINGS_BY_KEY.get(k)
        if not spec:
            errors[k] = "неизвестная настройка"
            continue
        try:
            applied[k] = _coerce(spec, v)
        except ValueError as e:
            errors[k] = str(e)
    cfg = app.cfg
    for k, v in applied.items():
        setattr(cfg, k, v)
    if applied:
        cfg.save()
    for k, v in applied.items():
        if k in C.PREDICTOR_KEYS:
            setattr(app.predictor, k, v)
        if k in LLM_CONFIGURE:
            app.llm.configure(**{LLM_CONFIGURE[k]: v})
        if k == "accept_key":
            app.shared.accept_right = v == "right"
            app.key_hint = "→" if app.shared.accept_right else "Tab"
    if applied and hasattr(app, "_apply_predictor_cfg"):  # min_insert, быстрый набор, кэш, раскладка
        app._apply_predictor_cfg()
    if "overlay_theme" in applied and hasattr(getattr(app, "overlay", None), "set_theme"):
        app.overlay.set_theme(applied["overlay_theme"])
    if applied.keys() & {"llm_enabled", "llm_only_on_ac"} and hasattr(app, "_manage_llm"):
        app._manage_llm(refresh_power=True)
    return {"applied": applied, "errors": errors,
            "restart": [k for k in applied if SETTINGS_BY_KEY[k].get("restart")]}


def settings_payload(app) -> dict:
    cfg = app.cfg
    return {"schema": SETTINGS, "values": {s["key"]: getattr(cfg, s["key"], None) for s in SETTINGS}}


# --- чёрный список ---
def set_blacklist(app, names) -> list[str]:
    """Заменить cfg.blacklist (нижний регистр, без дублей). ГЛАВНЫЙ ПОТОК."""
    if not isinstance(names, list) or len(names) > 500:
        raise PanelError("blacklist: нужен список")
    out: list[str] = []
    for n in names:
        if not isinstance(n, str):
            raise PanelError("blacklist: только строки")
        n = n.strip().lower()
        if not n:
            continue
        if len(n) > 100 or any(c in n for c in '\\/:*?"<>|\r\n'):
            raise PanelError(f"blacklist: странное имя программы {n[:30]!r}")
        if n not in out:
            out.append(n)
    app.cfg.blacklist = out
    app.cfg.save()
    app.blocked = getattr(app, "exe", "") in out
    return out


# --- словарь, запрещённые слова, сниппеты ---
def _word(body: dict) -> str:
    w = body.get("word")
    if not isinstance(w, str) or not w.strip():
        raise PanelError("нужно слово")
    w = w.strip().lower()
    if len(w) > 30 or not WORD_RE.fullmatch(w):
        raise PanelError("это не слово")
    return w


def dict_payload(app, prefix: str = "", limit: int = 200) -> dict:
    """Слова, набранные руками (UserModel.uni): по префиксу, самые частые. ГЛАВНЫЙ ПОТОК."""
    prefix = prefix.strip().lower()[:30]
    uni = app.predictor.user.uni
    items = [(w, n) for w, n in uni.items() if w.startswith(prefix)]
    items.sort(key=lambda x: (-x[1], x[0]))
    return {"words": [{"word": w, "n": n} for w, n in items[:limit]], "found": len(items), "total": len(uni),
            "banned": sorted(app.cfg.banned_words), "snippets": dict(app.cfg.snippets)}


def dict_forget(app, word: str) -> int:
    return app.predictor.user.forget(word)


def _set_banned(app, words: list[str]):
    app.cfg.banned_words = words
    app.cfg.save()
    app.predictor.banned = set(words)


def dict_ban(app, word: str):
    word = word.lower()
    if word not in app.cfg.banned_words:
        _set_banned(app, list(app.cfg.banned_words) + [word])


def dict_unban(app, word: str):
    _set_banned(app, [w for w in app.cfg.banned_words if w != word.lower()])


def snippets_edit(app, body: dict) -> dict:
    sn = dict(app.cfg.snippets)
    if "delete" in body:
        if not isinstance(body["delete"], str):
            raise PanelError("delete: нужна строка")
        sn.pop(body["delete"], None)
    else:
        abbr, text = body.get("abbr"), body.get("text")
        if not isinstance(abbr, str) or not isinstance(text, str):
            raise PanelError("нужны abbr и text")
        abbr = abbr.strip()
        if not abbr or len(abbr) > 40 or any(c.isspace() for c in abbr):
            raise PanelError("сокращение: 1–40 символов без пробелов")
        if not text.strip() or len(text) > 2000:
            raise PanelError("текст: от 1 до 2000 символов")
        sn[abbr] = text
    app.cfg.snippets = sn
    app.cfg.save()
    if hasattr(app, "_apply_predictor_cfg"):
        app._apply_predictor_cfg()
    return sn


# --- песочница ---
def sandbox(app, text: str) -> dict:
    """Что подскажет n-граммная модель на этот текст (LLM не участвует). ГЛАВНЫЙ ПОТОК."""
    p = app.predictor
    t0 = time.perf_counter()
    s = p.suggest(text)
    ms = (time.perf_counter() - t0) * 1000
    c2, c1, prefix, _start, _sp = parse_context(text)
    dist, sup = p.distribution(c2, c1, prefix)
    return {"suggestion": None if not s else {"insert": s.insert, "word": s.word, "level": s.level,
                                               "confidence": round(s.confidence, 3), "whole_word": s.whole_word},
            "ms": round(ms, 2), "context": {"c2": c2, "c1": c1, "prefix": prefix},
            "top": [{"word": p.surface(w), "p": round(pr, 4), "support": sup.get(w, 0)} for w, pr in dist[:8]]}


# --- статус ---
def process_memory_mb() -> float | None:
    """Рабочий набор процесса (WorkingSetSize), МБ; None — не Windows или не вышло."""
    try:
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        ps = ctypes.WinDLL("psapi")
        ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        ps.GetProcessMemoryInfo.restype = wintypes.BOOL
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return None
        return round(pmc.WorkingSetSize / 1048576, 1)
    except Exception:  # noqa: BLE001
        return None


def train_state(app) -> dict | None:
    tr = getattr(app, "trainer", None)
    return dict(tr.state) if tr is not None else None


def build_status(app) -> dict:
    """Лёгкий статус для вкладки «Обзор» и SSE. ГЛАВНЫЙ ПОТОК."""
    t = app.stats.summary(1)
    st = {"enabled": bool(app.shared.enabled), "llm_enabled": bool(app.cfg.llm_enabled),
          "llm_status": app._llm_status(), "mem_mb": process_memory_mb(),
          "today": {k: t[k] for k in ("shown", "accepted", "saved")}}
    tr = train_state(app)
    if tr is not None:
        st["train"] = tr
    return st


def overview(app) -> dict:
    s = app.stats
    return {"today": s.summary(1), "d7": s.summary(7), "d30": s.summary(30), "daily": s.daily(30)}


# --- обучение ---
def _train_action(app, name: str) -> bool:
    """start/apply/rollback: методы App, если есть, иначе trainer. ГЛАВНЫЙ ПОТОК."""
    tr = getattr(app, "trainer", None)
    app_method = {"start": "_retrain", "apply": "_train_apply", "rollback": "_train_rollback"}.get(name)
    fn = getattr(app, app_method, None) if app_method else None
    if fn is None and tr is not None:
        tm = {"start": "start", "cancel": "cancel", "apply": "apply_pending", "rollback": "rollback"}[name]
        fn = (lambda: tr.start(reason="manual")) if name == "start" else getattr(tr, tm, None)
    if fn is None:
        raise PanelError("обучение недоступно в этой сборке", 501)
    return fn() is not False


def train_payload(app) -> dict:
    cfg, state = call_main(app, lambda: (copy.copy(app.cfg), train_state(app)))
    out = {"available": getattr(app, "trainer", None) is not None, "state": state, "sources": [], "history": []}
    try:  # чтение файлов — в потоке сервера, главный цикл не держим
        from .trainer import history, sources_info
    except ImportError:
        return out
    try:
        out["sources"] = sources_info(cfg)
        out["history"] = history(20)
    except Exception:  # noqa: BLE001
        log.exception("панель: не прочитал источники/историю обучения")
        out["error"] = "не удалось прочитать источники или историю"
    return out


# --- сервер ---
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # на Windows SO_REUSEADDR даёт занять чужой порт


class PanelServer:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.stop_event = threading.Event()
        self._usage_cache: dict = {}
        base = int(getattr(app.cfg, "panel_port", 8768))
        tries = PORT_TRIES if base else 1  # 0 — порт выберет система (для тестов)
        err: OSError | None = None
        self.httpd = None
        for port in range(base, base + tries):
            try:
                self.httpd = _Server(("127.0.0.1", port), _make_handler(self))
                break
            except OSError as e:
                err = e
        if self.httpd is None:
            raise OSError(f"порты {base}…{base + tries - 1} заняты") from err
        self.port = self.httpd.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                       name="panel", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def stop(self):
        self.stop_event.set()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass

    def usage_rows(self, days: int) -> list[dict]:
        """Журнал подсказок за days дней; перед чтением сбрасываем буфер App на диск."""
        hit = self._usage_cache.get(days)
        if hit and time.time() - hit[0] < USAGE_TTL_S:
            return hit[1]
        from . import usage
        call_main(self.app, self.app.usage.flush)
        rows = usage.load(C.USAGE_FILE, days)
        self._usage_cache[days] = (time.time(), rows)
        return rows

    # --- GET ---
    def get_route(self, path: str, q: dict):
        app = self.app
        if path == "/api/status":
            return call_main(app, lambda: build_status(app))
        if path == "/api/overview":
            return call_main(app, lambda: overview(app))
        if path == "/api/train":
            return train_payload(app)
        if path == "/api/usage":
            from . import usage
            days = 30 if q.get("days") == "30" else 7
            return {"days": days, **usage.summary(self.usage_rows(days))}
        if path == "/api/settings":
            return call_main(app, lambda: settings_payload(app))
        if path == "/api/blacklist":
            return self.blacklist_payload()
        if path == "/api/dict":
            prefix = q.get("prefix", "")
            return call_main(app, lambda: dict_payload(app, prefix))
        raise PanelError("нет такого адреса", 404)

    def blacklist_payload(self) -> dict:
        app = self.app
        cfg_bl, nc = call_main(app, lambda: (list(app.cfg.blacklist), app.stats.no_caret_apps(30)))
        shown: dict[str, int] = {}
        for r in self.usage_rows(30):
            a = (r.get("app") or "").lower()
            if a:
                shown[a] = shown.get(a, 0) + 1
        names = set(shown) | {k.lower() for k in nc}
        recent = [{"exe": n, "shown": shown.get(n, 0), "no_caret": nc.get(n, 0), "blocked": n in cfg_bl}
                  for n in names]
        recent.sort(key=lambda r: (-r["shown"], -r["no_caret"], r["exe"]))
        return {"blacklist": cfg_bl, "recent": recent[:60]}

    # --- POST ---
    def post_route(self, path: str, body: dict):
        app = self.app
        if path == "/api/enabled":
            on = _flag(body)
            call_main(app, lambda: app._set_enabled(on))
            return call_main(app, lambda: build_status(app))
        if path == "/api/llm":
            on = _flag(body)

            def f():
                if bool(app.cfg.llm_enabled) != on:
                    app._toggle_llm()
                return build_status(app)
            return call_main(app, f)
        if path.startswith("/api/train/"):
            name = path.rsplit("/", 1)[1]
            if name not in ("start", "cancel", "apply", "rollback"):
                raise PanelError("нет такого адреса", 404)
            if name == "cancel":
                tr = getattr(app, "trainer", None)
                if tr is None:
                    raise PanelError("обучение недоступно в этой сборке", 501)
                return {"ok": bool(call_main(app, tr.cancel))}
            return {"ok": bool(call_main(app, lambda: _train_action(app, name)))}
        if path == "/api/settings":
            ch = body.get("changes")
            if not isinstance(ch, dict):
                raise PanelError("нужен объект changes")
            return call_main(app, lambda: apply_config(app, ch))
        if path == "/api/blacklist":
            names = body.get("blacklist")
            return {"blacklist": call_main(app, lambda: set_blacklist(app, names))}
        if path == "/api/sandbox":
            text = body.get("text")
            if not isinstance(text, str):
                raise PanelError("нужен text")
            return call_main(app, lambda: sandbox(app, text[-SANDBOX_MAX:]))
        if path == "/api/dict/forget":
            w = _word(body)
            return {"removed": call_main(app, lambda: dict_forget(app, w))}
        if path == "/api/dict/ban":
            w = _word(body)
            call_main(app, lambda: dict_ban(app, w))
            return {"ok": True}
        if path == "/api/dict/unban":
            w = _word(body)
            call_main(app, lambda: dict_unban(app, w))
            return {"ok": True}
        if path == "/api/snippets":
            return {"snippets": call_main(app, lambda: snippets_edit(app, body))}
        raise PanelError("нет такого адреса", 404)


def _flag(body: dict) -> bool:
    on = body.get("on")
    if not isinstance(on, bool):
        raise PanelError("нужно on: true/false")
    return on


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def _make_handler(panel: PanelServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "halfword-panel"

        def log_message(self, *args):  # без записи запросов: в путях могут быть слова из словаря
            pass

        # --- ответы ---
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _fail(self, status: int, msg: str):
            self._json({"error": msg}, status)

        # --- проверки ---
        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in panel.hosts

        def _cookie_ok(self) -> bool:
            try:
                c = SimpleCookie(self.headers.get("Cookie", ""))
            except Exception:  # noqa: BLE001
                return False
            m = c.get("st_token")
            return bool(m) and _same(m.value, panel.token)

        def _header_ok(self) -> bool:
            return _same(self.headers.get("X-Token", ""), panel.token)

        def _guard(self, post: bool) -> bool:
            if not self._host_ok():
                self._fail(403, "чужой Host")
                return False
            if post:
                origin = self.headers.get("Origin")
                if origin is not None and origin not in panel.origins:
                    self._fail(403, "чужой Origin")
                    return False
                if not self._header_ok():
                    self._fail(403, "нужен X-Token")
                    return False
            elif not (self._cookie_ok() or self._header_ok()):
                self._fail(403, "нет доступа: откройте панель из меню в трее")
                return False
            return True

        def _run(self, fn, *args):
            try:
                self._json(fn(*args))
            except PanelError as e:
                self._fail(e.status, str(e))
            except TimeoutError:
                self._fail(503, "программа занята, повторите")
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception:  # noqa: BLE001
                log.exception("панель: ошибка запроса %s", self.path.split("?")[0])
                self._fail(500, "внутренняя ошибка")

        # --- GET ---
        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._host_ok():
                return self._fail(403, "чужой Host")
            if u.path == "/" and "t" in q:  # вход по ссылке из трея: кладём cookie и убираем токен из адреса
                if not _same(q["t"], panel.token):
                    return self._fail(403, "неверный токен")
                return self._send(302, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"st_token={panel.token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._guard(False):
                return
            if u.path == "/":
                try:
                    page = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", panel.token)
                except OSError:
                    return self._fail(500, "нет web/index.html")
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if u.path in STATIC:
                try:
                    return self._send(200, (WEB_DIR / u.path[1:]).read_bytes(), STATIC[u.path])
                except OSError:
                    return self._fail(404, "нет файла")
            if u.path == "/api/events":
                return self._events()
            self._run(panel.get_route, u.path, q)

        # --- POST ---
        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            # тело читаем до ответа (не больше 1 МБ): иначе клиент, ещё отправляющий его, получает разрыв (WinError 10053)
            raw = self.rfile.read(min(n, 1 << 20)) if n > 0 else b""
            if not self._guard(True):
                return
            if n < 0:
                return self._fail(400, "Content-Length")
            if n > MAX_BODY:
                return self._fail(413, "слишком большой запрос")
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._fail(400, "нужен JSON")
            if not isinstance(body, dict):
                return self._fail(400, "нужен JSON-объект")
            self._run(panel.post_route, urlsplit(self.path).path, body)

        # --- SSE ---
        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            app = panel.app
            try:
                self.wfile.write(b"retry: 3000\n\n")
                while not panel.stop_event.is_set():
                    try:
                        data = call_main(app, lambda: build_status(app))
                    except TimeoutError:
                        data = {"error": "timeout"}
                    except Exception:  # noqa: BLE001
                        log.exception("панель: статус для SSE")
                        data = {"error": "internal"}
                    self.wfile.write(b"event: status\ndata: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    if panel.stop_event.wait(1.0):
                        break
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass  # клиент ушёл

    return Handler


def start(app) -> PanelServer:
    """Запустить панель; PanelServer.url — адрес с токеном (его открывает пункт трея), .stop() — остановка."""
    p = PanelServer(app)
    p.thread.start()
    log.info("панель: http://127.0.0.1:%s/ (токен на этот запуск — в пункте трея)", p.port)
    return p
