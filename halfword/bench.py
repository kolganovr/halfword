"""Стенд n-грамм с храповиком: экономия нажатий и доля верных на отложенных сообщениях + скорость suggest.

python -X utf8 -m halfword.bench                    # прогнать и сравнить с data/bench_baseline.json
python -X utf8 -m halfword.bench --save-baseline    # записать базу
python -X utf8 -m halfword.bench --set k=4          # гипотеза: атрибуты Predictor (база не обновляется)
python -X utf8 -m halfword.bench --notes <папка>    # добавить корпус заметок (каждый 10-й файл — проверка)
python -X utf8 -m halfword.bench --speed            # задержка suggest на фразе: base_model.pkl + user_model.json
python -X utf8 -m halfword.bench --synthetic        # то же на выдуманном корпусе, база в tests/fixtures/

Отложенная выборка — по времени: последние 10% сообщений Claude Code и Telegram. Граница и конец выборки
запоминаются в базе, поэтому новые сообщения не сдвигают её и прогоны сравнимы. Модель учится на остальном так же,
как переобучение (веса и полураспад из config.json). Печатаются только числа: текст сообщений в вывод не попадает.
Код выхода 1 — хуже базы сверх допуска.
"""
from __future__ import annotations

import argparse
import json
import pickle
import random
import statistics
import subprocess
import sys
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from . import config as C
from .corpus import claude_messages, iter_telegram, read_text_file, telegram_messages, text_files
from .evaluate import simulate
from .model import BaseModel, Predictor, UserModel
from .trainer import is_holdout

HOLDOUT_SHARE = 0.1
NOTES_EVERY = 10
CHARS = 50_000            # потолок проверочного текста на корпус
TOL_SAVED = 0.2           # п.п. экономии
TOL_PREC = 1.0            # п.п. доли верных
TOL_P95 = 0.2             # +20% к p95 suggest (и не меньше 0.2 мс — иначе это шум таймера)
SPEED_PHRASE = ("Сегодня я хочу написать короткое сообщение про работу и потом проверить "
                "как это всё работает на практике")
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bench_baseline.json"


def data_baseline() -> Path:
    return Path(C.DATA_DIR) / "bench_baseline.json"


# --- выдуманный корпус: для теста-храповика и отладки без личных данных ---
_SLOTS = {
    "greet": ["привет", "добрый день", "доброе утро", "здравствуйте", "салют"],
    "q": ["как дела", "как ты", "что нового", "как прошло", "ты где", "всё в силе"],
    "reply": ["спасибо", "отлично", "хорошо", "понял", "договорились", "ок", "супер"],
    "after": ["всё получилось", "всё работает", "посмотрю позже", "отвечу вечером", "сделаю завтра",
              "так и сделаем", "напишу потом"],
    "want": ["я хочу", "мне нужно", "надо бы", "я планирую", "давай", "попробую", "хочу"],
    "verb": ["проверить", "отправить", "написать", "обсудить", "посмотреть", "исправить", "обновить",
             "запустить", "доделать", "переделать"],
    "past": ["проверил", "отправил", "написал", "обсудил", "посмотрел", "исправил", "обновил", "доделал"],
    "obj": ["отчёт", "документ", "сообщение", "письмо", "задачу", "таблицу", "код", "презентацию", "черновик"],
    "objs": ["отчёты", "документы", "сообщения", "письма", "задачи", "таблицы", "тесты", "настройки"],
    "when": ["сегодня", "завтра", "вечером", "после обеда", "на неделе", "в понедельник", "в пятницу",
             "через час", "утром", "на выходных"],
    "state": ["готов", "почти готов", "ещё не готов", "в работе", "на проверке"],
    "thing": ["отчёт", "документ", "проект", "черновик", "план", "сервер", "скрипт"],
    "send": ["отправлю", "пришлю", "покажу", "скину"],
    "meet": ["созвонимся", "встретимся", "обсудим это", "увидимся", "спишемся"],
    "fix": ["исправить", "протестировать", "запустить", "обновить", "перезапустить", "настроить", "проверить"],
    "sys": ["сервер", "скрипт", "модель", "приложение", "панель", "бота", "базу", "сборку"],
    "sysn": ["сервер", "скрипт", "бот", "сайт", "стенд", "поиск"],
    "stateb": ["упал", "работает", "тормозит", "перезапущен", "обновлён", "молчит"],
    "ask": ["проверь", "посмотри", "прочитай", "поправь", "глянь"],
    "adj": ["мой", "этот", "новый", "последний", "второй"],
    "draft": ["вариант", "план", "черновик", "список", "текст", "отчёт"],
    "why": ["потому что", "так как", "ведь", "иначе"],
    "prob": ["ничего не работает", "тесты падают", "сборка не проходит", "сервер не отвечает", "всё зависает",
             "время уходит", "данные теряются"],
    "food": ["кофе", "чай", "обед", "пиццу", "суп"],
}
_STYLES = {
    "chat": ["{greet}, {q}?", "{reply}, {after}", "{want} {verb} {obj} {when}", "давай {meet} {when}",
             "{thing} {state}, {send} {when}", "пойдём на {food} {when}?", "я {when} {past} {objs}",
             "{reply}! {q}?"],
    "work": ["{ask}, пожалуйста, {adj} {draft}", "нужно {fix} {sys}, {why} {prob}",
             "сначала {fix} {sys}, потом {verb} {objs}", "{sysn} {stateb}", "почему {sysn} {stateb}?",
             "сделай {obj} {when}, {why} {prob}", "{ask} {adj} {draft} и {verb} {obj}",
             "{want} {fix} {sys} {when}"],
}


def _pick(rng: random.Random, items: list[str]) -> str:
    # частоты по Ципфу: первые варианты встречаются чаще
    return rng.choices(items, weights=[1 / (i + 1) ** 0.8 for i in range(len(items))])[0]


def synthetic_messages(style: str, n: int, seed: int = 1) -> list[tuple[datetime, str]]:
    """n выдуманных сообщений стиля chat или work: (время, текст), старые первыми."""
    rng = random.Random(f"{style}-{seed}")
    t0 = datetime(2026, 1, 1)
    out = []
    for i in range(n):
        parts = []
        for _ in range(rng.choice((1, 1, 2, 2, 3))):
            tpl = _pick(rng, _STYLES[style])
            s = tpl.format(**{k: _pick(rng, v) for k, v in _SLOTS.items()})
            parts.append(s)
        text = ". ".join(parts)
        if rng.random() < 0.5:
            text = text[0].upper() + text[1:]
        out.append((t0 + timedelta(minutes=7 * i), text))
    return out


# --- корпуса ---
def _ts(when) -> str:
    return when.isoformat() if isinstance(when, datetime) else str(when)


def _split(msgs: list, saved: dict | None) -> tuple[list, list, dict]:
    """msgs — [(время, текст)] по возрастанию. → (обучение, проверка, {cut, end}).

    Без сохранённой границы — последние HOLDOUT_SHARE по числу сообщений; с ней — то же окно по времени."""
    if not msgs:
        return [], [], {}
    if saved:
        cut, end = saved["cut"], saved["end"]
    else:
        cut, end = _ts(msgs[int(len(msgs) * (1 - HOLDOUT_SHARE))][0]), _ts(msgs[-1][0])
    train = [m for m in msgs if _ts(m[0]) < cut]
    test = [m for m in msgs if cut <= _ts(m[0]) <= end]
    return train, test, {"cut": cut, "end": end}


def load_corpora(cfg, base_split: dict | None, notes: str | None = None, synthetic: bool = False,
                 chars: int = CHARS) -> tuple[list, dict, dict]:
    """→ (обучающие тексты [(текст, вес)], {корпус: проверочный текст}, {корпус: служебное — граница, объём})."""
    base_split = base_split or {}
    if synthetic:
        cl = synthetic_messages("work", 2500)
        tg = synthetic_messages("chat", 2500)
    else:
        cl = [(when, t) for when, t in claude_messages(Path(C.CLAUDE_DIR))]
        tg = telegram_messages(Path(C.TELEGRAM_DIR))
    train: list = []
    tests: dict[str, str] = {}
    meta: dict[str, dict] = {}

    parts: dict[str, tuple[int, int]] = {}  # источник → его кусок в train (для множителей train.w_<источник>)
    tg_train, tg_test, sp = _split(tg, base_split.get("telegram"))
    if tg_test:
        n0 = len(train)
        train += list(iter_telegram(Path("."), cfg.tg_half_life_years, cfg.tg_weight, messages=tg_train))
        parts["telegram"] = (n0, len(train))
        tests["telegram"] = "\n".join(t for _w, t in tg_test)[:chars]
        meta["telegram"] = {"split": sp, "train": len(tg_train), "test": len(tg_test)}

    cl_train, cl_test, sp = _split(cl, base_split.get("claude"))
    if cl_test:
        if cfg.claude_weight > 0:
            n0 = len(train)
            train += [(t, cfg.claude_weight) for _w, t in cl_train]
            parts["claude"] = (n0, len(train))
        tests["claude"] = "\n".join(t for _w, t in cl_test)[:chars]
        meta["claude"] = {"split": sp, "train": len(cl_train), "test": len(cl_test)}

    if notes:
        files = text_files([notes])
        tr = [f for root, f in files if not is_holdout(f, root, NOTES_EVERY)]
        te = [f for root, f in files if is_holdout(f, root, NOTES_EVERY)]
        train += [(read_text_file(f), 1.0) for f in tr]
        tests["notes"] = "\n".join(read_text_file(f) for f in te)[:chars]
        meta["notes"] = {"split": {}, "train": len(tr), "test": len(te)}
    meta["_parts"] = {"split": {}, "parts": parts}
    for name, text in tests.items():
        meta[name]["chars"] = len(text)
        meta[name]["crc"] = zlib.crc32(text.encode("utf-8"))  # совпадает — значит проверочный текст тот же
    return train, tests, meta


# --- предсказатель с настройками, как в приложении ---
def make_predictor(base: BaseModel, cfg, overrides: dict | None = None) -> Predictor:
    p = Predictor(base, UserModel())
    for k in C.PREDICTOR_KEYS:
        setattr(p, k, getattr(cfg, k))
    p.min_insert = cfg.min_insert_chars
    p.next_min_tri = cfg.next_min_tri
    p.doc_weight = 3.0 if cfg.doc_cache else 0.0
    p.banned = {w.lower() for w in cfg.banned_words}
    for k, v in (overrides or {}).items():
        if not hasattr(p, k):
            raise SystemExit(f"у Predictor нет атрибута {k}")
        setattr(p, k, v)
    return p


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def measure(pred_factory, text: str, rows: list | None = None) -> dict:
    """Метрики одного корпуса: числа, без текста. rows — сюда же показы (см. evaluate.simulate)."""
    rows = [] if rows is None else rows
    times = []
    r = simulate(pred_factory(), text, rows=rows, times=times)
    word = [x for x in rows if x[0]]
    nxt = [x for x in rows if not x[0]]

    def prec(g):
        return round(100 * sum(1 for x in g if x[5]) / len(g), 2) if g else 0.0

    return {
        "saved": round(100 * r["saved"] / r["chars"], 3) if r["chars"] else 0.0,
        "shown_1k": round(1000 * r["shown"] / r["chars"], 1) if r["chars"] else 0.0,
        "prec": round(100 * r["accepted"] / r["shown"], 2) if r["shown"] else 0.0,
        "shown": r["shown"], "accepted": r["accepted"],
        "word_shown": len(word), "word_prec": prec(word),
        "word_saved": sum(x[6] for x in word),
        "next_shown": len(nxt), "next_prec": prec(nxt),
        "next_saved": sum(x[6] for x in nxt),
        "p50_ms": round(statistics.median(times), 3) if times else 0.0,
        "p95_ms": round(_pct(times, 0.95), 3), "max_ms": round(max(times), 2) if times else 0.0,
    }


def train_model(train: list, opts: dict | None = None, parts: dict | None = None) -> tuple[BaseModel, dict]:
    """opts — параметры BaseModel.train; w_<источник> — множитель веса его текстов (telegram, claude)."""
    opts = dict(opts or {})
    for src, (a, b) in (parts or {}).items():
        mult = opts.pop("w_" + src, None)
        if mult is not None:
            train = train[:a] + [(t, w * mult) for t, w in train[a:b]] + train[b:]
    t0 = time.perf_counter()
    base = BaseModel.train(train, **opts)
    secs = time.perf_counter() - t0
    size = len(pickle.dumps({"uni": base.uni, "bi": base.bi, "tri": base.tri, "case": base.case},
                            protocol=pickle.HIGHEST_PROTOCOL))
    return base, {"train_s": round(secs, 1), "words": len(base.vocab), "bi": len(base.bi), "tri": len(base.tri),
                  "size_mb": round(size / 2 ** 20, 2), "texts": len(train)}


def run(cfg, base_split: dict | None, overrides: dict | None = None, notes: str | None = None,
        synthetic: bool = False, chars: int = CHARS, train_opts: dict | None = None,
        corpora: tuple | None = None, model: tuple | None = None, rows: dict | None = None) -> dict:
    """Полный прогон: корпуса → модель → метрики. corpora/model — уже загруженные (для серии гипотез)."""
    train, tests, meta = corpora or load_corpora(cfg, base_split, notes, synthetic, chars)
    base, minfo = model or train_model(train, train_opts, meta.get("_parts", {}).get("parts"))
    out = {"model": minfo, "corpora": {}, "split": {k: v["split"] for k, v in meta.items() if v.get("split")}}
    for name, text in tests.items():
        got = rows.setdefault(name, []) if rows is not None else None
        m = measure(lambda: make_predictor(base, cfg, overrides), text, got)
        m.update(meta[name])
        m.pop("split", None)
        out["corpora"][name] = m
    return out


# --- сравнение с базой ---
def compare(cur: dict, base: dict) -> tuple[list[str], bool, bool]:
    """→ (строки отчёта, есть ухудшение сверх допуска, есть улучшение)."""
    lines, worse, better = [], False, False
    for name, m in cur["corpora"].items():
        b = base.get("corpora", {}).get(name)
        if not b:
            lines.append(f"{name}: в базе нет — не сравниваю")
            continue
        if (b.get("crc"), b.get("train")) != (m.get("crc"), m.get("train")):
            lines.append(f"{name}: корпус изменился (проверка {b.get('chars')}→{m.get('chars')} симв., "
                         f"обучение {b.get('train')}→{m.get('train')} сообщ.) — сравнение приблизительное")
        ds, dp = m["saved"] - b["saved"], m["prec"] - b["prec"]
        dt = m["p95_ms"] - b["p95_ms"]
        bad = []
        if ds < -TOL_SAVED:
            bad.append(f"экономия {ds:+.2f} п.п.")
        if dp < -TOL_PREC:
            bad.append(f"верных {dp:+.2f} п.п.")
        if b["p95_ms"] and dt > max(0.2, TOL_P95 * b["p95_ms"]):
            bad.append(f"p95 {b['p95_ms']:.2f}→{m['p95_ms']:.2f} мс")
        worse = worse or bool(bad)
        better = better or ds > 0.05 or (ds >= -0.01 and dp > 0.5)
        lines.append(f"{name}: экономия {b['saved']:.2f}→{m['saved']:.2f} ({ds:+.2f}), верных {b['prec']:.1f}→"
                     f"{m['prec']:.1f} ({dp:+.1f}), p95 {b['p95_ms']:.2f}→{m['p95_ms']:.2f} мс"
                     + ("  ХУЖЕ: " + ", ".join(bad) if bad else ""))
    return lines, worse, better


def report(res: dict) -> str:
    mi = res["model"]
    lines = [f"модель: обучение {mi['train_s']} с, слов {mi['words']}, биграмм {mi['bi']}, триграмм {mi['tri']}, "
             f"{mi['size_mb']} МБ, текстов {mi['texts']}"]
    for name, m in res["corpora"].items():
        lines.append(
            f"{name:9} {m['chars']:6} симв. | экономия {m['saved']:6.2f}% | показов на 1000 {m['shown_1k']:5.1f} | "
            f"верных {m['prec']:5.1f}% | в слове {m['word_shown']} ({m['word_prec']:.1f}%) | "
            f"след. слово {m['next_shown']} ({m['next_prec']:.1f}%) | suggest p50 {m['p50_ms']:.2f} "
            f"p95 {m['p95_ms']:.2f} max {m['max_ms']:.1f} мс")
    return "\n".join(lines)


def _commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parents[1],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def save_baseline(res: dict, path: Path, cfg):
    rec = dict(res, when=datetime.now().isoformat(timespec="seconds"), commit=_commit(),
               config={k: getattr(cfg, k) for k in (*C.PREDICTOR_KEYS, "min_insert_chars", "next_min_tri",
                                                    "doc_cache", "tg_half_life_years", "tg_weight",
                                                    "claude_weight")})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


# --- скорость на фразе ---
def speed(pred: Predictor, phrase: str = SPEED_PHRASE, repeats: int = 3) -> dict:
    """suggest на каждом префиксе фразы, repeats раз, перед каждым повтором — холодный кэш униграмм."""
    times = []
    for _ in range(repeats):
        pred._uni_cache.clear()
        for i in range(1, len(phrase) + 1):
            t0 = time.perf_counter()
            pred.suggest(phrase[:i])
            times.append((time.perf_counter() - t0) * 1000)
    return {"n": len(times), "p50_ms": round(statistics.median(times), 3), "p95_ms": round(_pct(times, 0.95), 3),
            "max_ms": round(max(times), 2), "total_ms": round(sum(times), 1)}


def _load_cfg(synthetic: bool):
    if synthetic:
        cfg = C.Config()
        cfg.claude_weight = 4.0  # второй выдуманный корпус учится, как сообщения Claude с весом из замеров
        return cfg
    if not C.CONFIG_FILE.exists():
        return C.Config()  # Config.load() без файла записал бы его
    return C.Config.load()


def _parse_sets(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="halfword.bench")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--notes")
    ap.add_argument("--chars", type=int, default=CHARS)
    ap.add_argument("--set", action="append", default=[], help="атрибут Predictor=значение (JSON)")
    ap.add_argument("--train", action="append", default=[], help="параметр BaseModel.train=значение (JSON)")
    ap.add_argument("--try", dest="tries", action="append", default=[],
                    help="серия гипотез в одном прогоне: \"a=1;b=2\", параметр обучения — \"train.x=1\"")
    ap.add_argument("--rows", help="записать показы (числа: в слове, уверенность, длина префикса, уровень, "
                                   "длина вставки, верно, сэкономлено) в этот JSON — для подбора порогов")
    ap.add_argument("--jobs", type=int, default=1, help="серия гипотез в N процессах")
    ap.add_argument("--brief", action="store_true", help="серия: строка на гипотезу и корпус")
    ap.add_argument("--resplit", action="store_true", help="с --save-baseline: новая граница выборки по свежим данным")
    ap.add_argument("--speed", action="store_true")
    ap.add_argument("--baseline", help="путь к базе (по умолчанию data/ или tests/fixtures для --synthetic)")
    a = ap.parse_args(argv)
    cfg = _load_cfg(a.synthetic)
    overrides, topts = _parse_sets(a.set), _parse_sets(a.train)

    if a.speed:
        if a.synthetic or not C.BASE_MODEL.exists():
            train, _t, _m = load_corpora(cfg, None, synthetic=True)
            base = BaseModel.train(train)
            user = UserModel()
        else:
            base, user = BaseModel.load(C.BASE_MODEL), UserModel.load(C.USER_MODEL)
        p = make_predictor(base, cfg, overrides)
        p.user = user
        print(json.dumps(speed(p), ensure_ascii=False))
        return 0

    path = Path(a.baseline) if a.baseline else (FIXTURE if a.synthetic else data_baseline())
    base = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    split = None if (base is None or a.resplit) else base.get("split")  # граница выборки живёт в базе
    if a.tries:
        return _series(cfg, split, base, a)
    rows = {} if a.rows else None
    res = run(cfg, split, overrides, a.notes, a.synthetic, a.chars, topts, rows=rows)
    print(report(res))
    if a.rows:
        Path(a.rows).write_text(json.dumps(rows), encoding="utf-8")
    if a.save_baseline or base is None:
        save_baseline(res, path, cfg)
        print(f"база записана: {path.name}")
        return 0
    lines, worse, better = compare(res, base)
    print("\n".join(lines))
    if worse:
        print("ХУЖЕ базы сверх допуска")
        return 1
    if better and not overrides and not topts:
        save_baseline(res, path, cfg)
        print("лучше базы — база обновлена")
    return 0


_W: dict = {}


def _worker_init(cfg, split, notes, synthetic, chars):
    _W.update(cfg=cfg, split=split, corpora=load_corpora(cfg, split, notes, synthetic, chars), models={})


def _worker_run(spec: str) -> dict:
    """Одна гипотеза: "a=1;b=2"; параметры обучения — с префиксом train."""
    sets = _parse_sets([x for x in spec.split(";") if x])
    topts = {k[6:]: v for k, v in sets.items() if k.startswith("train.")}
    over = {k: v for k, v in sets.items() if not k.startswith("train.")}
    key = json.dumps(topts, sort_keys=True)
    if key not in _W["models"]:
        _W["models"][key] = train_model(_W["corpora"][0], topts, _W["corpora"][2].get("_parts", {}).get("parts"))
    return run(_W["cfg"], _W["split"], over, corpora=_W["corpora"], model=_W["models"][key])


def _brief(spec: str, res: dict, base: dict | None) -> str:
    out = []
    for name, m in res["corpora"].items():
        b = (base or {}).get("corpora", {}).get(name)
        d = f" ({m['saved'] - b['saved']:+.2f} / {m['prec'] - b['prec']:+.1f})" if b else ""
        out.append(f"{spec or 'как есть':42} {name:8} экономия {m['saved']:6.2f} верных {m['prec']:5.1f}{d} | "
                   f"на 1000 {m['shown_1k']:5.1f} | в слове {m['word_shown']} {m['word_prec']:.1f}% "
                   f"{m['word_saved']} | след. {m['next_shown']} {m['next_prec']:.1f}% {m['next_saved']}")
    return "\n".join(out)


def _series(cfg, split, base, a) -> int:
    """Несколько гипотез на одних корпусах: модель переобучается, только если меняются параметры обучения."""
    specs = ["", *a.tries]
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(a.jobs, initializer=_worker_init,
                                 initargs=(cfg, split, a.notes, a.synthetic, a.chars)) as ex:
            results = ex.map(_worker_run, specs)
            for spec, res in zip(specs, results):
                _print_res(spec, res, base, a.brief)
        return 0
    _worker_init(cfg, split, a.notes, a.synthetic, a.chars)
    for spec in specs:
        _print_res(spec, _worker_run(spec), base, a.brief)
    return 0


def _print_res(spec: str, res: dict, base: dict | None, brief: bool):
    if brief:
        print(_brief(spec, res, base))
    else:
        print(f"=== {spec or 'как есть'}")
        print(report(res))
        if base:
            print("\n".join(compare(res, base)[0]))
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
