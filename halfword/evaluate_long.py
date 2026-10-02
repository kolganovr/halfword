"""Метрика режима паузы: насколько длинное продолжение совпадает с тем, что было написано дальше.

python -X utf8 -m halfword.evaluate_long [мест_на_тип] [модель.gguf] [--tg]

Места берутся в отложенных заметках (каждая 10-я) трёх типов: после конца предложения («sent»),
в начале строки («line») и после пробела посреди предложения («space»). Контекст — до 2000 символов перед местом.
Для каждого места: жадное продолжение + ветки (без шапки) и жадное продолжение с шапкой «Obsidian — имя заметки».
--tg: места в свежих 10% своих сообщений Telegram; контекст — свои предыдущие сообщения в том же чате
(как буфер, который Enter не стирает), «line» — начало сообщения, шапка «Telegram — название чата».
Текст сообщений не печатается — только числа.
Токены записываются один раз, стоп-правила (min_tok_p) перебираются офлайн.

Метрики:
  показ      — доля мест, где есть подсказка; слов — средняя длина подсказки;
  1-е слово  — доля мест, где первое слово верное;
  слов подряд — среднее число верных слов подряд с начала (столько примешь по Ctrl+→);
  экономия   — сэкономленные нажатия при приёме по словам до первой ошибки (каждый Ctrl+→ — нажатие),
               в % от длины правды до конца строки.
"""
from __future__ import annotations

import math
import random
import re
import sys
import time

from . import config as C
from . import llm as L
from .corpus import read_text_file, telegram_chat_messages, text_files

MIN_TOK_P = (0.0, 0.02, 0.05, 0.1)
KINDS = ("sent", "line", "space")


def holdout_notes(every: int = 10) -> list[tuple[str, str]]:
    files = [f for _root, f in text_files(C.text_roots(C.Config.load()))]
    return [(f.stem, read_text_file(f)) for i, f in enumerate(files) if i % every == 0]


def holdout_telegram(share: float = 0.1) -> list[tuple[str, str, int]]:
    """Свежие сообщения: (чат, свои предыдущие сообщения в чате + это сообщение, где оно начинается)."""
    msgs = telegram_chat_messages(C.TELEGRAM_DIR)
    if not msgs:
        raise SystemExit(f"нет сообщений в {C.TELEGRAM_DIR}")
    times = sorted(w for _c, w, _t in msgs)
    cut = times[int(len(times) * (1 - share))]
    out, hist = [], {}
    for chat, when, txt in sorted(msgs, key=lambda x: (x[0], x[1])):
        h = hist.get(chat, "")
        doc = (h + "\n" if h else "") + txt
        if when >= cut:
            out.append((chat, doc, len(doc) - len(txt)))
        hist[chat] = doc[-2000:]
    return out


def sample(notes, n: int, seed: int = 11, tg: bool = False):
    rnd = random.Random(seed)
    cands = {k: [] for k in KINDS}
    for item in notes:
        name, text = item[:2]
        start = item[2] if tg else 0
        for m in re.finditer(r"(?<=[.!?…] )\S|(?<=\n)\S|(?<=[^.!?…\n] )\S", text):
            pos = m.start()
            if pos < start:
                continue
            prev = text[pos - 1]
            kind = "line" if prev == "\n" else "sent" if text[pos - 2] in ".!?…" else "space"
            truth = text[pos:].split("\n", 1)[0]
            if (pos < 150 and not tg) or len(truth.split()) < (2 if tg else 3):
                continue
            if kind == "sent" and not text[pos - 3].isalpha():
                continue  # «1. », даты и сокращения с цифрами — не конец предложения
            cands[kind].append((kind, name, text[max(0, pos - 2000):pos], truth))
    return [x for k in KINDS for x in rnd.sample(cands[k], min(n, len(cands[k])))]


def ctrl_right_score(insert: str, truth: str) -> tuple[int, int]:
    """→ (слов подряд верно, сэкономлено нажатий) при приёме по Ctrl+→ до первой ошибки."""
    acc, words, saved = "", 0, 0
    for chunk in re.findall(r"\s*\S+", insert):
        nxt = acc + chunk
        if not truth.startswith(nxt) or (len(truth) > len(nxt) and not truth[len(nxt)].isspace()):
            break
        acc = nxt
        words += 1
        saved += max(0, len(chunk) - 1)
    return words, saved


def variants_for(trace: list[dict], min_tok_p: float, max_words: int):
    """Записанные токены → [(вставка, оценка)]: сначала жадная, потом ветки."""
    out = []
    for t in trace:
        ins, _s, lp, k = L.long_cut(t["tokens"], t["forced"], min_tok_p, max_words, t["ended"])
        if t["kind"] == "greedy":
            out.append((ins, lp / max(k, 1)))
        else:
            full = (t["forced"][len(t["lead"]):] + ins).rstrip()
            out.append((full, (math.log(max(t["pa"], 1e-6)) + lp) / (1 + k)))
    return [v for v in out if v[0].strip()]


def report(rows, key: str, title: str, max_words: int):
    print(f"\n== {title}")
    print(f"{'тип':6} {'min_p':>5} {'показ':>6} {'слов':>5} {'1-е слово':>9} {'подряд':>7} {'экономия':>8}"
          f" | {'лучший из 3':>11} {'ранж.':>6}")
    for kind in KINDS + ("всё",):
        rs = [r for r in rows if kind in ("всё", r["kind"]) and r.get(key) is not None]
        if not rs:
            continue
        truth_len = sum(len(r["truth"]) for r in rs)
        for mp in MIN_TOK_P:
            shown = words = first = run = saved = best = ranked = 0
            for r in rs:
                vs = variants_for(r[key], mp, max_words)
                if not vs:
                    continue
                g = vs[0][0]
                shown += 1
                words += len(g.split())
                w, sv = ctrl_right_score(g, r["truth"])
                first += w > 0
                run += w
                saved += sv
                best += max(ctrl_right_score(v, r["truth"])[1] for v, _s in vs)
                ranked += ctrl_right_score(max(vs, key=lambda x: x[1])[0], r["truth"])[1]
            n = len(rs)
            print(f"{kind:6} {mp:5.2f} {shown / n:6.0%} {words / max(shown, 1):5.1f} {first / n:9.0%}"
                  f" {run / n:7.2f} {saved / truth_len:8.1%} | {best / truth_len:11.1%} {ranked / truth_len:6.1%}")


def main():
    tg = "--tg" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--tg"]
    n = int(args[0]) if args else 20
    model = args[1] if len(args) > 1 else C.Config().llm_model
    cfg = C.Config.load()
    app = "Telegram" if tg else "Obsidian"
    src = holdout_telegram() if tg else holdout_notes()
    rows = [dict(kind=k, name=nm, ctx=ctx, truth=tr) for k, nm, ctx, tr in sample(src, n, tg=tg)]
    counts = ", ".join(f"{k} {sum(r['kind'] == k for r in rows)}" for k in KINDS)
    print(f"мест: {len(rows)} ({counts})")
    eng = L.LLM(model, port=8795, ctx=cfg.llm_ctx)
    eng.configure(keep_long=cfg.llm_prompt_chars, long_tokens=cfg.llm_long_tokens, long_words=cfg.llm_long_words,
                  long_min_tok_p=0.0, variants=cfg.llm_variants)
    eng.start()
    while not eng.ready:
        time.sleep(0.2)
    t0 = time.time()
    first_ms = []
    try:
        for i, r in enumerate(rows):
            for key, header, variants in (("plain", "", cfg.llm_variants), ("header", f"{app} — {r['name']}", 1)):
                eng.trace, eng.variants, eng._prev_start = [], variants, None
                prefix = ""
                eng._complete_long(L.Job("long", 0, r["ctx"], prefix, lambda _r: None, header))
                r[key] = eng.trace
                if key == "plain" and eng.trace and eng.trace[0].get("first_ms"):
                    first_ms.append(eng.trace[0]["first_ms"])
            if i % 10 == 9:
                print(f"  {i + 1}/{len(rows)} за {time.time() - t0:.0f} с", flush=True)
    finally:
        eng.stop()
    first_ms.sort()
    if first_ms:
        print(f"первое слово (с холодным промптом ~{cfg.llm_prompt_chars} симв.): медиана {first_ms[len(first_ms) // 2]:.0f} мс")
    report(rows, "plain", "без шапки: жадный вариант (слева) и 3 варианта (справа)", cfg.llm_long_words)
    report(rows, "header", f"с шапкой «{app} — {'чат' if tg else 'имя заметки'}»: жадный вариант", cfg.llm_long_words)
    if tg:
        return  # переписку не печатаем: вывод попадает в лог сессии
    print("\nпримеры (sent):")
    for r in [r for r in rows if r["kind"] == "sent"][:6]:
        vs = variants_for(r["plain"], 0.0, cfg.llm_long_words)
        print(f"  …{r['ctx'][-50:]!r}\n     правда: {r['truth'][:70]!r}")
        for v, s in vs:
            print(f"     {s:6.2f} {v[:70]!r}")


if __name__ == "__main__":
    main()
