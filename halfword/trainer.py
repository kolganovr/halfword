"""Переобучение базовой модели в отдельном процессе: этапы, ворота качества, откат, история.

Процесс хука модель не обучает: n-граммы считает дочерний процесс (spawn, приоритет ниже обычного),
на этапах шлёт события в очередь; поток-наблюдатель в процессе приложения принимает их, решает про ворота,
подменяет файл и пишет строку в data/train_runs.jsonl. Тексты корпуса в историю не попадают.
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

from . import config as C
from .corpus import text_files

log = logging.getLogger("halfword")

HOLDOUT_EVERY = 40        # каждый 40-й текстовый файл не обучает модель, а проверяет её
HOLDOUT_CHARS = 20_000    # объём проверочного текста
CHECK_SECS = 30           # потолок на быструю проверку
EMIT_EVERY = 0.5          # события о ходе — не чаще 2 раз/с
BELOW_NORMAL = 0x4000     # BELOW_NORMAL_PRIORITY_CLASS


# --- пути (читаются в момент вызова: тесты подменяют C.*) ---
def _paths() -> dict[str, str]:
    base = Path(C.BASE_MODEL)
    return {"texts": [str(p) for p in C.text_roots(C.Config.load())], "telegram": str(C.TELEGRAM_DIR),
            "claude": str(C.CLAUDE_DIR), "base": str(base),
            "new": str(base.with_name(base.stem + ".new" + base.suffix)),
            "prev": str(base.with_name(base.stem + ".prev" + base.suffix))}


def _runs_file() -> Path:
    return Path(C.DATA_DIR) / "train_runs.jsonl"


def is_holdout(path: Path, root: Path, every: int) -> bool:
    """Отложенный файл: по хешу относительного пути, а не по номеру — новые заметки не сдвигают выборку,
    и метрики разных запусков сравнимы."""
    rel = path.relative_to(root).as_posix().lower()
    return zlib.crc32(rel.encode("utf-8")) % every == 0


def _tg_files(folder: Path) -> list[Path]:
    return list(folder.rglob("result.json")) if folder.is_dir() else []


# --- история ---
def history(n: int = 20) -> list[dict]:
    """Последние n записей train_runs.jsonl, новые первыми."""
    p = _runs_file()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out[::-1][:n]


def _append_run(rec: dict):
    p = _runs_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _last_metric(tag: str) -> float | None:
    """Метрика последней принятой модели на той же выборке; откат (rollback) отменяет последнюю принятую."""
    skip = 0
    for r in history(10_000):
        if r.get("result") == "rollback":
            skip += 1
        elif r.get("result") == "accepted" and r.get("holdout") == tag and r.get("metric") is not None:
            if skip:
                skip -= 1
                continue
            return r["metric"]
    return None


def sources_info(cfg) -> list[dict]:
    """[{key,label,count,enabled}] — число файлов по источникам, тексты не читаются."""
    notes = len(text_files(C.text_roots(cfg)))
    tg = len(_tg_files(Path(C.TELEGRAM_DIR)))
    cl_dir = Path(C.CLAUDE_DIR)
    claude = len(list(cl_dir.rglob("*.jsonl"))) if cl_dir.is_dir() else 0
    return [
        {"key": "notes", "label": "Тексты", "count": notes, "enabled": notes > 0},
        {"key": "telegram", "label": "Telegram", "count": tg, "enabled": tg > 0},
        {"key": "claude", "label": "Сообщения Claude", "count": claude,
         "enabled": claude > 0 and getattr(cfg, "claude_weight", 0) > 0},
    ]


# --- дочерний процесс ---
def _set_low_priority():
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL)
    except Exception:
        pass


class _Emitter:
    """События не чаще EMIT_EVERY; смена этапа — всегда."""

    def __init__(self, q):
        self.q, self.last, self.stage = q, 0.0, ""

    def __call__(self, stage: str, label: str, pct: float, force: bool = False):
        now = time.monotonic()
        if force or stage != self.stage or now - self.last >= EMIT_EVERY:
            self.q.put({"type": "stage", "stage": stage, "label": label, "pct": int(pct)})
            self.last, self.stage = now, stage


def _lerp(lo: float, hi: float, i: int, n: int) -> float:
    return lo + (hi - lo) * (i / n if n else 1)


def _worker(job: dict, q):
    """Цель процесса (на уровне модуля — под spawn). Только пути и dict'ы: ни App, ни tk."""
    try:
        _set_low_priority()
        _run(job, q)
    except BaseException as e:  # noqa: BLE001 — в очередь уходит любая причина
        try:
            q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass


def _run(job: dict, q):
    from .corpus import claude_messages, iter_telegram, read_text_file, telegram_messages
    from .evaluate import simulate
    from .model import BaseModel, Predictor, UserModel

    P, cfg, every = job["paths"], job["cfg"], job["holdout_every"]
    emit = _Emitter(q)
    src: dict[str, dict] = {}
    texts: list = []

    # 1. тексты из папок (отложенные файлы в обучение не идут, их читаем отдельно для проверки)
    files = text_files(P["texts"])
    train_files = [f for root, f in files if not is_holdout(f, root, every)]
    hold_files = [f for root, f in files if is_holdout(f, root, every)]
    src["notes"] = {"files": len(train_files), "holdout": len(hold_files)}
    emit("notes", f"Тексты, {len(train_files)} файлов", 0, force=True)
    for i, f in enumerate(train_files, 1):
        try:
            texts.append(read_text_file(f))
        except OSError:
            pass
        emit("notes", f"Тексты, {i} из {len(train_files)}", _lerp(0, 22, i, len(train_files)))

    # 2. Telegram
    tg_dir = Path(P["telegram"])
    tg_files = _tg_files(tg_dir)
    if not tg_files:
        src["telegram"] = {"messages": 0, "bytes": 0}
        emit("telegram", "Telegram: не найден", 22, force=True)
    else:
        emit("telegram", f"Telegram, {len(tg_files)} экспорт(ов)", 22, force=True)
        nbytes = sum(p.stat().st_size for p in tg_files)
        try:
            msgs = telegram_messages(tg_dir)
            items = list(iter_telegram(tg_dir, cfg["tg_half_life_years"], cfg["tg_weight"], messages=msgs))
            texts.extend(items)
            src["telegram"] = {"messages": len(items), "bytes": nbytes}
        except Exception as e:  # битый экспорт не должен ронять всё обучение
            src["telegram"] = {"messages": 0, "bytes": nbytes, "error": f"{type(e).__name__}"}
        emit("telegram", f"Telegram, {src['telegram']['messages']} сообщений", 32, force=True)

    # 3. сообщения Claude Code
    if cfg["claude_weight"] > 0 and Path(P["claude"]).is_dir():
        emit("claude", "Сообщения Claude", 32, force=True)
        cm = claude_messages(Path(P["claude"]))
        texts.extend((t, cfg["claude_weight"]) for _when, t in cm)
        src["claude"] = {"messages": len(cm)}
    else:
        src["claude"] = {"messages": 0}
        emit("claude", "Claude: нет данных", 32, force=True)
    emit("claude", "Сообщения Claude", 40, force=True)

    # 4. подсчёт n-грамм
    total = len(texts)

    def progress(stage, n):
        if stage == "count":
            emit("count", f"Подсчёт слов, {n} из {total}", _lerp(40, 80, n, total))
        else:
            emit("prune", "Отсев редких сочетаний", 82, force=True)

    emit("count", f"Подсчёт слов, {total} текстов", 40, force=True)
    model = BaseModel.train(texts, progress)
    del texts
    emit("write", "Запись модели", 88, force=True)
    model.save(Path(P["new"]))

    # 5. быстрая проверка на отложенных текстах
    metric, hold_chars = None, 0
    emit("check", "Проверка качества", 90, force=True)
    if hold_files:
        pred = Predictor(model, UserModel())
        for k in C.PREDICTOR_KEYS:
            setattr(pred, k, cfg[k])
        deadline = time.monotonic() + CHECK_SECS
        saved = chars = 0
        for i, f in enumerate(hold_files, 1):
            room = HOLDOUT_CHARS - chars
            if room <= 0 or time.monotonic() > deadline:
                break
            try:
                t = read_text_file(f)[:room]
            except OSError:
                continue
            if not t.strip():
                continue
            r = simulate(pred, t, learn=False)
            saved += r["saved"]
            chars += r["chars"]
            emit("check", "Проверка качества", _lerp(90, 99, chars, HOLDOUT_CHARS))
        hold_chars = chars
        metric = round(saved / chars * 100, 3) if chars else None
    q.put({"type": "result", "words": len(model.uni), "metric": metric, "holdout_chars": hold_chars,
           "sources": src})


# --- сторона приложения ---
class Trainer:
    def __init__(self, on_event):
        self.on_event = on_event
        self.holdout_every = HOLDOUT_EVERY
        self._lock = threading.Lock()
        self._proc = None
        self._cancelled = False
        last = history(1)
        self.state: dict = {"running": False, "stage": "", "label": "", "pct": 0, "started": None,
                            "last": last[0] if last else None, "pending": False}
        self._refresh_pending()

    # --- служебное ---
    def _emit(self, ev: dict):
        try:
            self.on_event(ev)
        except Exception:
            log.exception("on_event обучения")

    def _refresh_pending(self):
        self.state["pending"] = Path(_paths()["new"]).exists()

    @staticmethod
    def _rm(*paths: str):
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def last_run_ts(self) -> float:
        """Когда начался последний завершённый запуск (иначе — mtime модели, иначе 0)."""
        for r in history(100):
            if r.get("result") in ("accepted", "rejected"):
                return float(r.get("ts", 0))
        try:
            return Path(C.BASE_MODEL).stat().st_mtime
        except OSError:
            return 0.0

    # --- запуск/отмена ---
    def start(self, reason: str = "manual") -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            cfg = C.Config.load()
            P = _paths()
            self._rm(P["new"], str(Path(P["new"]).with_suffix(".tmp")))
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            job = {"paths": P, "holdout_every": self.holdout_every,
                   "cfg": {k: getattr(cfg, k) for k in (*C.PREDICTOR_KEYS, "tg_half_life_years", "tg_weight",
                                                       "claude_weight")}}
            proc = ctx.Process(target=_worker, args=(job, q), daemon=True)
            t0 = time.time()
            self._cancelled = False
            self.state.update(running=True, stage="start", label="Запуск", pct=0, started=t0, pending=False)
            proc.start()
            self._proc = proc
            threading.Thread(target=self._watch, args=(proc, q, job, t0, reason, cfg.train_gate_pp),
                             daemon=True, name="trainer-watch").start()
        self._emit({"type": "stage", "stage": "start", "label": "Запуск", "pct": 0})
        return True

    def cancel(self) -> bool:
        with self._lock:
            proc = self._proc
            if not self.state["running"] or proc is None:
                return False
            self._cancelled = True
            try:
                proc.terminate()
            except Exception:
                log.exception("не остановил процесс обучения")
        return True

    def _watch(self, proc, q, job, t0, reason, gate_pp):
        result = error = None
        while result is None and error is None:
            try:
                msg = q.get(timeout=0.3)
            except queue.Empty:
                if proc.is_alive():
                    continue
                try:  # процесс закончился — забрать то, что успел положить
                    msg = q.get(timeout=0.3)
                except queue.Empty:
                    break
            kind = msg.get("type")
            if kind == "stage":
                self.state.update(stage=msg["stage"], label=msg["label"], pct=msg["pct"])
                self._emit(msg)
            elif kind == "result":
                result = msg
            elif kind == "error":
                error = msg["error"]
        proc.join(5)
        if proc.is_alive():
            proc.terminate()
        secs = round(time.time() - t0, 1)
        P, tag = job["paths"], f"md/{job['holdout_every']}"
        rec = {"ts": round(t0, 2), "when": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
               "secs": secs, "reason": reason, "holdout": tag, "sources": None, "words": None,
               "metric": None, "prev_metric": None, "result": None, "error": None}
        ev = None
        try:
            if self._cancelled:
                self._rm(P["new"], str(Path(P["new"]).with_suffix(".tmp")))
                rec["result"], ev = "cancelled", {"type": "cancelled"}
            elif error is not None or result is None:
                self._rm(P["new"], str(Path(P["new"]).with_suffix(".tmp")))
                err = error or f"процесс обучения завершился (код {proc.exitcode})"
                rec.update(result="error", error=err)
                ev = {"type": "error", "error": err}
            else:
                metric, prev = result["metric"], _last_metric(tag)
                ok = metric is None or prev is None or metric >= prev - gate_pp
                rec.update(sources=result["sources"], words=result["words"], metric=metric, prev_metric=prev,
                           result="accepted" if ok else "rejected")
                if ok:
                    self._swap_in(P)
                ev = {"type": "done", "accepted": ok, "metric": metric, "prev_metric": prev,
                      "words": result["words"], "secs": secs}
        except Exception as e:
            log.exception("итог обучения")
            rec.update(result="error", error=f"{type(e).__name__}: {e}")
            ev = {"type": "error", "error": rec["error"]}
        try:
            _append_run(rec)
        except OSError:
            log.exception("не записал train_runs.jsonl")
        with self._lock:
            self._proc = None
            self.state.update(running=False, stage="", label="", pct=0, started=None, last=rec)
            self._refresh_pending()
        self._emit(ev)

    # --- файлы модели ---
    @staticmethod
    def _swap_in(P: dict):
        """base → prev, new → base (os.replace)."""
        if Path(P["base"]).exists():
            os.replace(P["base"], P["prev"])
        os.replace(P["new"], P["base"])

    def apply_pending(self) -> bool:
        with self._lock:
            P = _paths()
            if self.state["running"] or not Path(P["new"]).exists():
                return False
            self._swap_in(P)
            # отклонённый запуск становится принятым — теперь с него считаются ворота
            rej = next((r for r in history(100) if r.get("result") == "rejected"), None)
            if rej:
                rec = dict(rej, ts=round(time.time()), when=datetime.now().isoformat(timespec="seconds"),
                           reason="apply_pending", result="accepted", secs=0)
                _append_run(rec)
                self.state["last"] = rec
            self._refresh_pending()
        return True

    def rollback(self) -> bool:
        """base_model.prev.pkl ↔ base_model.pkl (повторный вызов возвращает всё обратно)."""
        with self._lock:
            P = _paths()
            if self.state["running"] or not Path(P["prev"]).exists():
                return False
            tmp = str(Path(P["base"]).with_suffix(".swap"))
            if Path(P["base"]).exists():
                os.replace(P["base"], tmp)
                os.replace(P["prev"], P["base"])
                os.replace(tmp, P["prev"])
            else:
                os.replace(P["prev"], P["base"])
            rec = {"ts": round(time.time()), "when": datetime.now().isoformat(timespec="seconds"),
                   "secs": 0, "reason": "rollback", "holdout": None, "sources": None, "words": None,
                   "metric": None, "prev_metric": None, "result": "rollback", "error": None}
            _append_run(rec)
            self.state["last"] = rec
        return True

    # --- нужен ли новый запуск ---
    def corpus_changed(self) -> bool:
        """Тексты правились / экспорт Telegram изменился / в Claude Code появились новые сессии после последнего запуска."""
        ref = self.last_run_ts()
        if not ref:
            return True
        ref += 1.0  # ts округлён, а файл могли сохранить за миг до запуска — он уже в модели
        try:
            for _root, f in text_files(C.text_roots(C.Config.load())):
                if f.stat().st_mtime > ref:
                    return True
            tg = _tg_files(Path(C.TELEGRAM_DIR))
            size = sum(p.stat().st_size for p in tg)
            last = next((r for r in history(100) if r.get("result") in ("accepted", "rejected")), None)
            old = ((last or {}).get("sources") or {}).get("telegram", {}).get("bytes")
            if old is not None and size != old:
                return True
            if old is None and size and last:  # раньше экспорта не было
                return True
            cl = Path(C.CLAUDE_DIR)
            if cl.is_dir() and C.Config.load().claude_weight > 0:
                for p in cl.rglob("*.jsonl"):
                    if p.stat().st_mtime > ref:
                        return True
        except OSError:
            return False
        return False

