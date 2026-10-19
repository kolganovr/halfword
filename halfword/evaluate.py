"""Офлайн-симуляция: «печатаем» отложенные заметки и считаем экономию нажатий при разных порогах.

python -X utf8 -m halfword.evaluate [макс_символов]
"""
from __future__ import annotations

import re
import sys
import time

from . import config as C
from . import morph
from . import noise as N
from .corpus import read_text_file, text_files
from .model import BaseModel, Predictor, UserModel, parse_context

SEP = " ,.;:!?)\n"


def split_corpus(holdout_every: int = 10):
    files = [f for _root, f in text_files(C.text_roots(C.Config.load()))]
    train, test = [], []
    for i, f in enumerate(files):
        (test if i % holdout_every == 0 else train).append(f)
    return [read_text_file(f) for f in train], [read_text_file(f) for f in test]


def simulate(pred: Predictor, text: str, learn: bool = True, rows: list | None = None,
             times: list | None = None, noise: float = 0.0, noise_seed: int = N.SEED) -> dict:
    """rows — сюда по каждому засчитанному показу: (в слове?, уверенность, длина префикса, уровень, длина вставки,
    верно?, сэкономлено) — только числа; times — длительность каждого suggest, мс.
    noise > 0 — доля слов с синтетическими опечатками (см. simulate_noisy); 0 — прежний путь без изменений."""
    if noise > 0:
        return simulate_noisy(pred, text, noise, noise_seed, learn, rows, times)
    shown = accepted = wrong = saved = wrong_form = 0
    last_wrong = None
    buf = ""
    i = 0
    n = len(text)
    clock = time.perf_counter
    count_form = morph.available()
    while i < n:
        if buf.strip():
            if times is not None:
                t0 = clock()
                s = pred.suggest(buf)
                times.append((clock() - t0) * 1000)
            else:
                s = pred.suggest(buf)
        else:
            s = None
        if s:
            rest = text[i:]
            tail = rest[len(s.insert):len(s.insert) + 1]
            ok = rest.startswith(s.insert) and (not s.whole_word or tail == "" or tail in SEP)
            if rows is not None and (ok or s.insert != last_wrong):
                plen = len(parse_context(buf)[2])
                gain = len(s.insert + (" " if s.whole_word and tail == " " else "")) - 1 if ok else 0
                rows.append((plen > 0, round(s.confidence, 4), plen, s.level, len(s.insert), ok, gain))
            if ok:
                accepted += 1
                shown += 1
                add = s.insert + (" " if s.whole_word and tail == " " else "")
                saved += len(add) - 1  # минус нажатие Tab
                for ch in add:
                    if ch in SEP and learn:
                        pred.learn_from(buf)
                    buf += ch
                i += len(add)
                last_wrong = None
                continue
            if s.insert != last_wrong:
                wrong += 1
                shown += 1
                last_wrong = s.insert
                if count_form and s.whole_word and _is_form_miss(buf, text, i, s.word):
                    wrong_form += 1
        ch = text[i]
        if ch == "\n":
            if learn:
                pred.learn_from(buf)
            buf = ""
        else:
            if ch in SEP and learn:
                pred.learn_from(buf)
            buf = (buf + ch)[-400:]
        i += 1
    return {"chars": n, "shown": shown, "accepted": accepted, "wrong": wrong, "saved": saved,
            "wrong_form": wrong_form}


_TAIL_WORD = re.compile(r"[^\W\d_]+$")


_HEAD_WORD = re.compile(r"[^\W\d_]+")


def _is_form_miss(buf: str, text: str, i: int, shown_word: str) -> bool:
    """Показано неверное слово, но его лемма та же, что у слова, которое дальше набрано (форма, не другое слово)."""
    prefix = parse_context(buf)[2]
    m = _HEAD_WORD.match(text, i)
    real = (prefix + (m.group() if m else "")).lower()
    return bool(real) and morph.same_lemma(shown_word.lower(), real)


def _lcp(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _after_autocorrect(ac, buf: str, word: str) -> str:
    """Слово после автозамены предиктора (то же слово, если замены нет)."""
    s = ac(buf)
    if not s or s.replace > len(word):
        return word
    return word[:len(word) - s.replace] + s.insert


def simulate_noisy(pred: Predictor, text: str, noise: float, seed: int = N.SEED, learn: bool = True,
                   rows: list | None = None, times: list | None = None) -> dict:
    """Набор с опечатками: часть слов (доля noise, от 3 букв) «пользователь» печатает испорченными, цель — text.

    Нажатия: каждый набранный символ, Tab на принятие подсказки (стирание replace делает приложение), Backspace и
    допечатка вручную. Неисправленную опечатку пользователь чинит после слова: Backspace до первого расхождения и
    допечатка верного хвоста. Автозамена pred.autocorrect(buf) (если есть) вызывается на разделителе и нажатий не
    стоит; чужая замена в другое слово и замена верного слова чинятся так же вручную.
    saved = len(text) − нажатия, как и в прежнем счёте (там это сумма вставок минус Tab)."""
    plan = N.plan(text, noise, seed)
    ac = getattr(pred, "autocorrect", None)
    st = {"typo_words": 0, "typo_avoided": 0, "fixed_ok": 0, "fixed_bad": 0, "typo_manual": 0, "false_fix": 0}
    shown = accepted = wrong = keys = 0
    last_wrong = None
    buf = ""
    i = 0
    n = len(text)
    typo = None  # [верное слово, набираемое, набрано символов] — слово с опечаткой, которое сейчас печатается
    skip_ac = False
    clock = time.perf_counter
    while i < n:
        if typo is None and i in plan:
            typo = [plan[i][0], plan[i][1], 0]
            st["typo_words"] += 1
        k = typo[2] if typo else 0
        tp = typo[1][:k] if typo else ""
        s = None
        if buf.strip():
            t0 = clock()
            s = pred.suggest(buf)
            if times is not None:
                times.append((clock() - t0) * 1000)
        if s:
            r = s.replace
            if r <= k:
                start, content = i, tp[:k - r] + s.insert
            else:
                start, content = i - (r - k), s.insert
            e = start + len(content)
            tail = text[e:e + 1]
            ok = start >= 0 and text.startswith(content, start) and (not s.whole_word or tail == "" or tail in SEP)
            sp = " " if s.whole_word and tail == " " else ""
            if rows is not None and (ok or s.insert != last_wrong):
                plen = len(parse_context(buf)[2])
                gain = len(s.insert + sp) - 1 if ok else 0
                rows.append((plen > 0, round(s.confidence, 4), plen, s.level, len(s.insert), ok, gain))
            if ok:
                accepted += 1
                shown += 1
                keys += 1  # Tab
                if typo:
                    st["typo_avoided" if text.startswith(tp, i) else "fixed_ok"] += 1
                buf = buf[:max(0, len(buf) - r)]
                for ch in s.insert + sp:
                    if ch in SEP and learn:
                        pred.learn_from(buf)
                    buf += ch
                i = e + len(sp)
                typo = None
                last_wrong = None
                skip_ac = False
                continue
            if s.insert != last_wrong:
                wrong += 1
                shown += 1
                last_wrong = s.insert
        if typo and k < len(typo[1]):  # печатаем следующую букву испорченного слова
            buf = (buf + typo[1][k])[-400:]
            typo[2] += 1
            keys += 1
            skip_ac = False
            continue
        if typo:  # слово набрано целиком: автозамена, иначе ручная правка
            clean, typed = typo[0], typo[1]
            got = _after_autocorrect(ac, buf, typed) if ac else typed
            if got == clean:
                st["fixed_ok"] += 1
            else:
                st["fixed_bad" if got != typed else "typo_manual"] += 1
                d = _lcp(got, clean)
                keys += len(got) - d + len(clean) - d
            buf = buf[:len(buf) - len(typed)] + clean
            i += len(clean)
            typo = None
            skip_ac = True
            continue
        ch = text[i]
        if ch in SEP and ac and not skip_ac:  # верное слово не должно меняться автозаменой
            m = _TAIL_WORD.search(buf)
            if m:
                got = _after_autocorrect(ac, buf, m.group())
                if got != m.group():
                    st["false_fix"] += 1
                    d = _lcp(got, m.group())
                    keys += len(got) - d + len(m.group()) - d
        keys += 1
        if ch == "\n":
            if learn:
                pred.learn_from(buf)
            buf = ""
        else:
            if ch in SEP and learn:
                pred.learn_from(buf)
            buf = (buf + ch)[-400:]
        i += 1
        skip_ac = False
    return {"chars": n, "shown": shown, "accepted": accepted, "wrong": wrong, "saved": n - keys,
            "words": len(N.WORD_RE.findall(text)), **st}


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000
    t0 = time.time()
    train, test = split_corpus()
    base = BaseModel.train(train)
    text = "\n".join(test)[:limit]
    print(f"обучение {time.time() - t0:.0f} с; тестовый текст {len(text):,} символов")
    grid = [
        ("текущие", dict()),
        ("смелее", dict(min_conf_word=0.25, min_conf_next=0.25, min_support=2, short_penalty=0.05)),
        ("ещё смелее", dict(min_conf_word=0.20, min_conf_next=0.15, min_support=2, short_penalty=0.04)),
        ("максимум", dict(min_conf_word=0.12, min_conf_next=0.10, min_support=1, short_penalty=0.03)),
    ]
    cfg = C.Config.load()
    for name, params in grid:
        p = Predictor(base, UserModel())
        for k in C.PREDICTOR_KEYS:  # «текущие» — пороги из config.json, как в работающем приложении
            setattr(p, k, getattr(cfg, k))
        for k, v in params.items():
            setattr(p, k, v)
        t1 = time.time()
        r = simulate(p, text)
        prec = r["accepted"] / r["shown"] if r["shown"] else 0
        print(f"{name:12} сэкономлено {r['saved'] / r['chars']:6.1%} нажатий | показов {r['shown']:5} "
              f"верных {prec:5.1%} | {time.time() - t1:.0f} с")


if __name__ == "__main__":
    main()
