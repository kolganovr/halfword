"""Маленькая LLM (llama.cpp llama-server) для продолжения фраз — только на зарядке.

Сервер — отдельный процесс: на батарее и после простоя его убиваем, память освобождается целиком.
Два режима: «набор» — короткая подсказка, пока совокупная вероятность выше порога;
«пауза» — продолжение до конца предложения потоком, без порога, потом ветки-варианты.
Соединение рвём, как только пришло новое нажатие.
"""
from __future__ import annotations

import ctypes
import http.client
import json
import logging
import math
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
LLM_DIR = _HOME / ".halfword" / "llm"
if not LLM_DIR.exists() and (_HOME / ".smart-type" / "llm").exists():  # прежнее имя проекта
    LLM_DIR = _HOME / ".smart-type" / "llm"
SERVER_EXE = LLM_DIR / "bin" / "llama-server.exe"
MODELS_DIR = LLM_DIR / "models"

_job_handle = None


def _kill_with_us(pid: int):
    """Job Object с KILL_ON_JOB_CLOSE: сервер умрёт вместе с нами, даже если нас убили без выхода."""
    global _job_handle
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    if _job_handle is None:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", ctypes.c_ulong), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_ulong),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_ulong),
                        ("SchedulingClass", ctypes.c_ulong)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("Job Object: SetInformation не удался (%s)", ctypes.get_last_error())
        _job_handle = job
    h = k32.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if h:
        if not k32.AssignProcessToJobObject(_job_handle, h):
            log.warning("Job Object: Assign не удался (%s)", ctypes.get_last_error())
        k32.CloseHandle(h)


CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
WORD_BOUNDARY = re.compile(r"\s")


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_ac_power() -> bool:
    st = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return True
    return st.ACLineStatus != 0  # 0 — батарея, 1 — сеть, 255 — неизвестно (десктоп)


SENT_END_RE = re.compile(r"[.!?…]+[»\"')\]]*$")


@dataclass
class LLMResult:
    seq: int
    text: str          # буфер, по которому считали
    insert: str        # что допечатать
    prob: float        # quick: совокупная вероятность вставки; long: средний logprob токена (оценка варианта)
    kind: str = "quick"   # quick — во время набора; long — в паузе
    variant: int = 0      # long: 0 — жадное продолжение, 1.. — ветки
    done: bool = True     # long: False — промежуточный ответ, подсказка ещё растёт
    off: int = 0          # app: абсолютное смещение text в буфере (буфер — скользящее окно)


@dataclass
class Job:
    kind: str
    seq: int
    text: str
    prefix: str
    callback: object
    header: str = ""
    min_prob: float = 0.4
    max_words: int = 4
    gen: int = 0


def prompt_window(text: str, prev_start: str | None, max_chars: int = 2000, keep: int = 350,
                  grow: bool = False):
    """Окно текста для промпта со стабильным началом, чтобы сервер переиспользовал KV-кэш.

    Сдвиг начала = пересчёт всего промпта (~8 мс на токен; у Gemma 3 KV при сдвиге не переиспользуется),
    поэтому окно держим, пока оно не длиннее max_chars. grow — режим паузы: окно можно расширить до keep,
    если прибавится больше 200 символов (пересчёт пройдёт, пока человек думает).
    """
    if prev_start:
        p = text.rfind(prev_start)
        cur = len(text) - p
        if p >= 0 and cur <= max_chars and not (grow and min(len(text), keep) - cur > 200):
            return text[p:], prev_start
    w = text[-keep:]
    if len(text) > keep:
        sp = w.find(" ")
        if 0 <= sp < 60:
            w = w[sp + 1:]
    return w, w[:40]


def split_lead(text: str, prefix: str) -> tuple[str, str]:
    """→ (промпт без недописанного слова и хвостового пробела, обязательное начало ответа).

    Обрубок слова в промпте сбивает токенизатор (в обучении «дела» — один токен « дела»),
    поэтому его убираем, а грамматикой заставляем модель начать ответ с « де».
    """
    base = text[: len(text) - len(prefix)] if prefix else text
    had_space = base.endswith(" ")
    return base.rstrip(" "), (" " if had_space else "") + prefix


def lead_grammar(lead: str) -> str:
    if not lead:  # начало строки: ответ не с пробела
        return 'root ::= [^\\n ] [^\\n]*'
    lit = lead.replace("\\", "\\\\").replace('"', '\\"')
    return f'root ::= "{lit}" [^\\n]*'


def norm_prob(p: float, tops: list[tuple[str, float]], remaining: str) -> float:
    """Вероятность токена среди совместимых с ещё не покрытой частью начала ответа."""
    if not remaining:
        return p
    mass = sum(q for t, q in tops if t and (t.startswith(remaining) or remaining.startswith(t)))
    # планка 0.02: если совместимых вариантов модель почти не видит, это не уверенность, а угадайка
    return min(1.0, p / max(mass, p, 0.02)) if p > 0 else 0.0


def build_insert(tokens: list[tuple[str, float]], lead: str, min_prob: float, max_words: int,
                 ended: bool = False):
    """Токены (уже с нормированными p) → (вставка после lead, вероятность).

    Режем по границе слова, пока совокупная уверенность выше порога.
    ended — генерация остановилась сама: последнее слово закончено.
    """
    n = len(lead)
    gen = ""
    cum = 1.0
    best = None
    words = 0

    def started() -> bool:  # набрано хоть что-то сверх обязательного начала
        return len(gen) > n and bool(gen[n:].strip())

    for tok, p in tokens:
        if "\n" in tok:
            piece = tok.split("\n")[0]
            if piece and cum * p >= min_prob:
                gen += piece
                cum *= p
            if started():
                best = (gen, cum)
            break
        if cum * p < min_prob:
            if WORD_BOUNDARY.match(tok) and started():
                best = (gen, cum)  # следующее слово сомнительно, но текущее точно закончено
            break
        if WORD_BOUNDARY.match(tok) and started():
            best = (gen, cum)
            words += 1
            if words >= max_words:
                break
        gen += tok
        cum *= p
    else:
        if ended and started():
            best = (gen, cum)
    if not best or not best[0].startswith(lead):
        return None
    ins = best[0][n:].rstrip()
    if not ins.strip():
        return None
    if lead.strip() and ins[:1].isspace():
        return None  # модель считает слово законченным — дописывать нечего
    return ins, best[1]


def _trim_repeat(ins: str) -> tuple[str, bool]:
    """Обрезать перед первой тройкой слов, которая уже встречалась (модель зациклилась)."""
    spans = [(m.start(), m.group(0).lower()) for m in re.finditer(r"\S+", ins)]
    seen = set()
    for i in range(len(spans) - 2):
        tri = tuple(w for _s, w in spans[i:i + 3])
        if tri in seen:
            return ins[:spans[i][0]].rstrip(), True
        seen.add(tri)
    return ins, False


def long_cut(tokens: list[tuple[str, float]], lead: str, min_tok_p: float = 0.0, max_words: int = 20,
             ended: bool = False) -> tuple[str, bool, float, int]:
    """Режим паузы: токены (нормированные p) → (вставка после lead, хватит, сумма logp, число токенов).

    Порога по совокупной вероятности нет: после точки он не проходит никогда.
    В вставку идут только законченные слова (плашка растёт по словам). Стоп: конец предложения,
    max_words, повтор трёх слов, токен с p < min_tok_p начиная с третьего слова, конец генерации.
    """
    n = len(lead)
    gen = ""
    words = 0
    lp, k = 0.0, 0
    cut = (n, 0.0, 0)
    stop = False
    for tok, p in tokens:
        if tok[:1].isspace() and gen[n:].strip():
            cut = (len(gen), lp, k)
            words += 1
            if SENT_END_RE.search(gen[n:]) or words >= max_words:
                stop = True
                break
        if words >= 2 and p < min_tok_p:
            stop = True
            break
        gen += tok
        if len(gen) > n:
            lp += math.log(max(p, 1e-6))
            k += 1
    else:
        if ended and gen[n:].strip():
            cut = (len(gen), lp, k)
            stop = True
    ins, rep = _trim_repeat(gen[n:cut[0]].rstrip())
    return ins, stop or rep, cut[1], cut[2]


def branch_alternatives(tops: list[tuple[str, float]], greedy: str, remaining: str, n: int):
    """Первые токены для веток: совместимы с началом ответа, дают новое слово, не совпадают с жадным."""
    out = []
    seen = {greedy.strip().lower()}
    for t, q in sorted(tops, key=lambda x: -x[1]):
        if not t or "\n" in t or not t.startswith(remaining) or len(t) <= len(remaining):
            continue
        if not remaining and t[:1].isspace() and not greedy[:1].isspace():
            continue
        if not re.search(r"\w", t[len(remaining):]) or greedy.startswith(t):
            continue
        key = t.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((t, norm_prob(q, tops, remaining)))
        if len(out) >= n:
            break
    return out


class LLM:
    def __init__(self, model_file: str, threads: int = 4, port: int = 8767, ctx: int = 2048):
        self.model = MODELS_DIR / model_file
        self.threads = threads
        self.port = port
        self.ctx = ctx
        # окно промпта (символы) и режим паузы; app перезаписывает из config
        self.keep_quick = 350
        self.keep_long = 1000
        self.max_prompt_chars = 2000
        self.long_tokens = 40
        self.long_words = 20
        self.long_min_tok_p = 0.0
        self.variants = 3
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self._lock = threading.Lock()
        self._want = threading.Condition()
        self._job: Job | None = None
        self._gen = 0                        # любое новое нажатие/запрос делает текущую генерацию устаревшей
        self._prev_start: str | None = None
        self.last_ms = 0.0
        self.trace: list | None = None       # evaluate_long: сюда пишутся токены режима паузы
        self._conn: http.client.HTTPConnection | None = None  # текущий запрос: при отмене рвём его сразу
        self.fails = 0                       # подряд неудачных запусков сервера
        self.retry_at = 0.0                  # раньше этого времени сервер не перезапускаем
        threading.Thread(target=self._worker, name="llm", daemon=True).start()

    def configure(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def available(self) -> bool:
        return SERVER_EXE.exists() and self.model.exists()

    # --- процесс сервера ---
    def start(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            if time.time() < self.retry_at:
                return  # сервер недавно упал — не перезапускать на каждое нажатие
            if not self.available:
                log.warning("LLM: нет %s или %s", SERVER_EXE, self.model)
                return
            logf = open(LLM_DIR / "server.log", "w", encoding="utf-8", errors="replace")
            self.proc = subprocess.Popen(
                [str(SERVER_EXE), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                 "-c", str(self.ctx), "-t", str(self.threads), "-np", "1"],
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
            self.ready = False
            self._prev_start = None
            try:
                _kill_with_us(self.proc.pid)
            except Exception:
                log.exception("Job Object")
            log.info("LLM: запускаю %s", self.model.name)
        threading.Thread(target=self._wait_ready, args=(self.proc,), daemon=True).start()

    def _failed(self, why: str):
        self.fails += 1
        delay = min(600, 15 * 2 ** (self.fails - 1))
        self.retry_at = time.time() + delay
        log.error("LLM: %s, см. %s; повтор не раньше чем через %s с", why, LLM_DIR / "server.log", delay)

    def _wait_ready(self, proc: subprocess.Popen):
        t0 = time.time()
        while time.time() - t0 < 60:
            if self.proc is not proc:
                return  # выгрузили или перезапустили, пока ждали
            if proc.poll() is not None:
                self._failed("сервер завершился")
                return
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                c.request("GET", "/health")
                if c.getresponse().status == 200:
                    with self._lock:
                        if self.proc is proc:
                            self.ready = True
                            self.fails = 0
                    log.info("LLM: готова за %.1f с", time.time() - t0)
                    return
            except OSError:
                pass
            time.sleep(0.3)
        self._failed("сервер не ответил за 60 с")

    def stop(self, reason: str = ""):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                log.info("LLM: выгружена%s", f" ({reason})" if reason else "")
            self.proc = None
            self.ready = False

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # --- запросы ---
    def request(self, seq: int, text: str, prefix: str, min_prob: float, max_words: int, callback,
                header: str = ""):
        """Быстрый запрос во время набора; предыдущий незавершённый отменяется."""
        self._submit(Job("quick", seq, text, prefix, callback, header, min_prob, max_words))

    def request_long(self, seq: int, text: str, prefix: str, callback, header: str = ""):
        """Запрос в паузе: callback получает растущее продолжение, потом ветки."""
        self._submit(Job("long", seq, text, prefix, callback, header))

    def _submit(self, job: Job):
        with self._want:
            self._gen += 1
            job.gen = self._gen
            self._job = job
            self._want.notify()
        self._abort()

    def cancel(self):
        with self._want:
            self._gen += 1
            self._job = None
        self._abort()

    def _abort(self):
        """Оборвать текущий запрос: иначе устаревание видно только с первым токеном, после разбора промпта."""
        c = self._conn
        if c is not None and c.sock is not None:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _worker(self):
        while True:
            with self._want:
                while self._job is None:
                    self._want.wait()
                job = self._job
                self._job = None
            if not self.ready:
                continue
            try:
                if job.kind == "long":
                    self._complete_long(job)
                    continue
                res = self._complete(job)
            except Exception as e:
                log.debug("LLM запрос: %s", e)
                res = None
            log.debug("LLM %.0f мс: %s", self.last_ms, "есть" if res else "ниже порога/отменён")
            if res is not None:
                job.callback(res)

    def _stale(self, job: Job) -> bool:
        return self._gen != job.gen

    def _prompt(self, base: str, header: str, grow: bool) -> str:
        w, self._prev_start = prompt_window(base, self._prev_start, self.max_prompt_chars,
                                            self.keep_long if grow else self.keep_quick, grow)
        return (header.strip() + "\n\n" if header and header.strip() else "") + w

    def _stream(self, job: Job, prompt: str, lead: str, n_predict: int, timeout: float, on_token):
        """Потоковая генерация с началом lead. on_token(tok, p_норм, tops, смещение) → True — хватит.

        → (ended, stale): ended — модель остановилась сама; stale — пришло новое нажатие.
        """
        body = json.dumps({"prompt": prompt, "n_predict": n_predict, "temperature": 0, "n_probs": 20,
                           "stream": True, "cache_prompt": True, "grammar": lead_grammar(lead)})
        t0 = time.perf_counter()
        if self._stale(job):
            return False, True
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        self._conn = c
        gen = ""
        try:
            c.request("POST", "/completion", body, {"Content-Type": "application/json"})
            if self._stale(job):
                return False, True  # отменили, пока отправляли (_abort мог не застать сокет)
            r = c.getresponse()
            for raw in r:
                if self._stale(job):
                    return False, True  # уже напечатали дальше — ответ не нужен
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                d = json.loads(line[5:])
                for tok, p, tops in _parse_tokens(d):
                    remaining = lead[len(gen):] if len(gen) < len(lead) else ""
                    off = len(gen)
                    gen += tok
                    if on_token(tok, norm_prob(p, tops, remaining), tops, off):
                        return False, False
                if d.get("stop"):
                    return d.get("stop_type") in ("word", "eos"), False
        except OSError:
            if self._stale(job):
                return False, True  # соединение оборвал _abort
            raise
        finally:
            self._conn = None
            c.close()  # закрытие соединения останавливает генерацию на сервере
            self.last_ms = (time.perf_counter() - t0) * 1000
        return False, self._stale(job)  # поток кончился: сам или его оборвал _abort

    def _complete(self, job: Job):
        base, lead = split_lead(job.text, job.prefix)
        if not lead:
            return None  # начало строки без пробела — в быстром режиме не гадаем
        prompt = self._prompt(base, job.header, grow=False)
        tokens: list[tuple[str, float]] = []
        cum = [1.0]

        def on(tok, p, _tops, _off):
            tokens.append((tok, p))
            cum[0] *= p
            return cum[0] < job.min_prob

        ended, stale = self._stream(job, prompt, lead, 10, 3, on)
        if stale:
            return None
        built = build_insert(tokens, lead, job.min_prob, job.max_words, ended)
        if not built:
            return None
        return LLMResult(job.seq, job.text, built[0], built[1])

    def _complete_long(self, job: Job):
        """Пауза: жадное продолжение потоком (плашка растёт), потом ветки от других первых токенов."""
        base, lead = split_lead(job.text, job.prefix)
        if not lead and not base.endswith("\n"):
            return
        prompt = self._prompt(base, job.header, grow=True)
        tokens: list[tuple[str, float]] = []
        fork: list = []
        shown = [""]
        t0 = time.perf_counter()
        first_ms = [0.0]

        def on(tok, p, tops, off):
            tokens.append((tok, p))
            if not fork and off + len(tok) > len(lead):
                fork.append((off, tok, tops))
            ins, stop, lp, k = long_cut(tokens, lead, self.long_min_tok_p, self.long_words)
            if ins.strip() and ins != shown[0]:
                shown[0] = ins
                first_ms[0] = first_ms[0] or (time.perf_counter() - t0) * 1000
                job.callback(LLMResult(job.seq, job.text, ins, lp / max(k, 1), "long", 0, False))
            return stop

        ended, stale = self._stream(job, prompt, lead, self.long_tokens, 20, on)
        if stale:
            return
        ins, _stop, lp, k = long_cut(tokens, lead, self.long_min_tok_p, self.long_words, ended)
        log.debug("LLM пауза %.0f мс, %s ток.", self.last_ms, len(tokens))
        if self.trace is not None:
            self.trace.append(dict(kind="greedy", lead=lead, forced=lead, tokens=list(tokens), ended=ended,
                                   pa=1.0, ms=self.last_ms, first_ms=first_ms[0]))
        if not ins.strip():
            return
        job.callback(LLMResult(job.seq, job.text, ins, lp / max(k, 1), "long", 0, True))
        if not fork or self.variants <= 1:
            return
        off, gtok, tops = fork[0]
        gen = "".join(t for t, _ in tokens)
        alts = branch_alternatives(tops, gtok, lead[off:] if off < len(lead) else "", self.variants - 1)
        for i, (alt, pa) in enumerate(alts, 1):
            forced = gen[:off] + alt
            btoks: list[tuple[str, float]] = []

            def onb(tok, p, _tops, _off, btoks=btoks, forced=forced):
                btoks.append((tok, p))
                return long_cut(btoks, forced, self.long_min_tok_p, self.long_words)[1]

            ended_b, stale = self._stream(job, prompt, forced, self.long_tokens, 20, onb)
            if stale:
                return
            if self.trace is not None:
                self.trace.append(dict(kind="branch", lead=lead, forced=forced, tokens=list(btoks), ended=ended_b,
                                       pa=pa, ms=self.last_ms))
            bi, _s, blp, bk = long_cut(btoks, forced, self.long_min_tok_p, self.long_words, ended_b)
            full = (forced[len(lead):] + bi).rstrip()
            if full.strip():
                score = (math.log(max(pa, 1e-6)) + blp) / (1 + bk)
                job.callback(LLMResult(job.seq, job.text, full, score, "long", i, True))


def _parse_tokens(d: dict):
    """→ (токен, p, [(альтернатива, p)]) ; разные версии llama-server кладут вероятности по-разному."""
    probs = d.get("completion_probabilities") or []
    if not probs:
        if d.get("content"):
            yield d["content"], 0.5, []  # без вероятностей — считаем средней уверенностью
        return
    for item in probs:
        tok = item.get("token", item.get("content", ""))
        if "logprob" in item:
            p = math.exp(item["logprob"])
            tops = [(x.get("token", ""), math.exp(x["logprob"])) for x in item.get("top_logprobs", [])]
        elif item.get("probs"):
            tops = [(x.get("tok_str", ""), x.get("prob", 0.0)) for x in item["probs"]]
            p = tops[0][1] if tops else 0.5
        else:
            p, tops = item.get("prob", 0.5), []
        yield tok, p, tops
