"""Порядок n-грамм: окно 1/2/3 слова на заметках и свежем Telegram. Печатает только числа.

python -X utf8 -m halfword.evaluate_order [символов_заметок] [символов_telegram]

Модель любого порядка с тем же suggest, что в model.py; на n=3 даёт ровно цифры model.py (строка «контроль»).
"""
from __future__ import annotations

import gc
import os
import pickle
import statistics
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from itertools import accumulate

from . import config as C
from . import model as M
from .corpus import iter_telegram, iter_texts, telegram_messages
from .evaluate import simulate, split_corpus

TOPS = {1: M.BI_TOP, 2: M.TRI_TOP, 3: 30}
MINS = {1: 0, 2: M.TRI_MIN, 3: 2}


class NBase:
    def __init__(self, n):
        self.n = n
        self.uni = {}
        self.ctx = {k: {} for k in range(1, n)}
        self.case = {}
        self.tokens = 0

    @classmethod
    def train(cls, texts, n):
        uni = Counter()
        flat = {k: Counter() for k in range(1, n)}   # "ctx\tw" → счёт
        tot = {k: Counter() for k in range(1, n)}    # ctx → полная масса
        surface = defaultdict(Counter)
        for item in texts:
            text, wt = (item, 1) if isinstance(item, str) else item
            for toks in M.sentences(text):
                for i, t in enumerate(toks):
                    if i > 0 and not t.isdigit():
                        surface[t.lower()][t] += wt
                seq = [M.BOS] * (n - 1) + [M.norm(t) for t in toks]
                for i in range(n - 1, len(seq)):
                    w = seq[i]
                    uni[w] += wt
                    for k in range(1, n):
                        c = " ".join(seq[i - k:i])
                        flat[k][c + "\t" + w] += wt
                        tot[k][c] += wt
        m = cls(n)
        m.uni = dict(uni)
        m.tokens = sum(uni.values())
        for k in range(1, n):
            groups = defaultdict(list)
            for key, cnt in flat[k].items():
                if cnt >= MINS[k]:
                    c, w = key.split("\t")
                    groups[c].append((w, cnt))
            flat[k] = None
            tab = {}
            for c, lst in groups.items():
                lst.sort(key=lambda x: -x[1])
                tab[c] = (tot[k][c], lst[:TOPS[k]])
            m.ctx[k] = tab
        for low, forms in surface.items():
            best, _ = forms.most_common(1)[0]
            if best != low:
                m.case[low] = best
        m._index()
        return m

    def _index(self):
        self.vocab = sorted(w for w in self.uni if not w.startswith("<"))
        self.cum = [0] + list(accumulate(self.uni[w] for w in self.vocab))

    def prefix_range(self, prefix):
        import bisect
        return bisect.bisect_left(self.vocab, prefix), bisect.bisect_left(self.vocab, prefix + "￿")

    def blob(self):
        return pickle.dumps({"uni": self.uni, "ctx": self.ctx, "case": self.case}, protocol=pickle.HIGHEST_PROTOCOL)


class NUser:
    def __init__(self, n):
        self.uni = Counter()
        self.ctx = {k: defaultdict(Counter) for k in range(1, n)}
        self.case = {}

    def learn(self, ctx, word):
        if len(word) > M.MAX_WORD_LEN:
            return
        w = M.norm(word)
        self.uni[w] += 1
        for k in self.ctx:
            self.ctx[k][" ".join(ctx[-k:])][w] += 1
        if word != w and not word.isdigit() and ctx[-1] != M.BOS:
            self.case[w] = word


def parse_ctx(text, n):
    tail = text[-300:]
    sent = M.SENT_SPLIT_RE.split(tail)[-1]
    m = M.TRAILING_WORD_RE.search(sent)
    prefix = m.group(0) if m else ""
    before = sent[: len(sent) - len(prefix)] if prefix else sent
    words = [M.norm(t) for t in M.WORD_RE.findall(before)]
    seq = [M.BOS] * (n - 1) + words
    ends_space = text[-1:].isspace() if text else True
    return seq[-(n - 1):], prefix, not words, ends_space


class NPredictor(M.Predictor):
    """Тот же suggest, что в model.py, но контекст — n-1 слов."""

    def __init__(self, base, n, kk=None):
        self.n = n
        self.kk = kk or {}
        super().__init__(base, None)
        self.user = NUser(n)
        self.times = []

    def _lvl(self, k, key, prefix):
        got, mass = Counter(), 0.0
        if k == 0:
            lo, hi = self.base.prefix_range(prefix)
            mass = float(self.base.cum[hi] - self.base.cum[lo])
            for w in self._uni_top(prefix):
                got[w] = self.base.uni[w]
            utab = self.user.uni
        else:
            table = self.base.ctx[k]
            if key in table:
                total, lst = table[key]
                for w, c in lst:
                    if w.startswith(prefix):
                        got[w] += c
                stored = sum(c for _, c in lst)
                matched = sum(got.values())
                mass = matched + (total - stored) * (matched / stored if stored else 0)
            utab = self.user.ctx[k].get(key, {})
        for w, c in utab.items():
            if w.startswith(prefix):
                got[w] += c * M.USER_WEIGHT
                mass += c * M.USER_WEIGHT
        for w in [w for w in got if w.startswith("<")]:
            del got[w]
        return got, mass

    def dist(self, ctx, prefix):
        p = prefix.lower()
        levels = [self._lvl(0, "", p)] + [self._lvl(k, " ".join(ctx[-k:]), p) for k in range(1, self.n)]
        words = set()
        for g, _ in levels:
            words |= set(g)
        lams = [m / (m + self.kk.get(k, self.k)) for k, (_g, m) in enumerate(levels)]
        probs = {}
        for w in words:
            g1, m1 = levels[0]
            pr = g1.get(w, 0) / m1 if m1 else 0.0
            for k in range(1, self.n):
                g, m = levels[k]
                pr = lams[k] * (g.get(w, 0) / m if m else 0.0) + (1 - lams[k]) * pr
            probs[w] = pr
        support = Counter()
        for k in range(1, min(2, self.n - 1) + 1):  # опора — би+триграммы, как в model.py
            support += levels[k][0]
        return sorted(probs.items(), key=lambda x: -x[1]), support

    def _next(self, ctx):
        out = []
        for _ in range(self.max_extra_words):
            d, sup = self.dist(ctx, "")
            if not d:
                break
            w, pr = d[0]
            if pr < self.min_conf_extra or sup[w] < self.min_support or w == M.NUM:
                break
            out.append(self.surface(w))
            ctx = ctx[1:] + [w]
        return out

    def suggest(self, text):
        t0 = time.perf_counter()
        try:
            return self._suggest(text)
        finally:
            self.times.append(time.perf_counter() - t0)

    def _suggest(self, text):
        ctx, prefix, sent_start, ends_space = parse_ctx(text, self.n)
        if prefix and (prefix[-1] in "-'’" or prefix.isdigit()):
            return None
        if not prefix and text and not ends_space:
            return None
        d, sup = self.dist(ctx, prefix)
        if not d:
            return None
        low = prefix.lower()
        w, pr = d[0]
        if w == low:
            return None
        if not prefix and (pr < self.min_conf_next or sup[w] < self.min_support or w == M.NUM):
            return None
        need = (self.min_conf_word + max(0, 4 - len(prefix)) * self.short_penalty) if prefix else self.min_conf_next
        if pr >= need:
            word = M.match_case(self.surface(w), prefix, sent_start)
            insert = word[len(prefix):]
            extra = self._next(ctx[1:] + [w])
            if len(insert) < 2 and not extra:
                return None
            if extra:
                insert += " " + " ".join(extra)
            return M.Suggestion(insert=insert, word=word, confidence=pr, level="ng", whole_word=True)
        if not prefix:
            return None
        group, cover = [], 0.0
        for cand, cp in d[:8]:
            group.append(cand)
            cover += cp
            if cover >= self.stem_cover:
                break
        if cover < self.stem_cover or low in group:
            return None
        stem = os.path.commonprefix(group)
        if len(stem) - len(low) < 2:
            return None
        stem = M.match_case(stem, prefix, sent_start)
        return M.Suggestion(insert=stem[len(prefix):], word=stem, confidence=cover, level="stem", whole_word=False)

    def learn_from(self, text_before_sep):
        ctx, prefix, _, _ = parse_ctx(text_before_sep.rstrip(), self.n)
        if prefix and not prefix.isdigit():
            self.user.learn(ctx, prefix)


def apply_cfg(p):
    cfg = C.Config.load()
    for k in C.PREDICTOR_KEYS:
        setattr(p, k, getattr(cfg, k))


def run(label, base, n, text, **kw):
    kk = kw.pop("kk", None)
    p = NPredictor(base, n, kk)
    apply_cfg(p)
    for k, v in kw.items():
        setattr(p, k, v)
    t0 = time.time()
    r = simulate(p, text)
    ts = sorted(p.times)
    prec = r["accepted"] / r["shown"] if r["shown"] else 0
    pct = lambda q: ts[min(len(ts) - 1, int(len(ts) * q))] * 1000
    print(f"  {label:26} экономия {r['saved'] / r['chars']:6.2%} | показов {r['shown']:5} верных {r['accepted']:5} "
          f"({prec:5.1%}) | suggest ср {statistics.mean(ts) * 1000:.2f} p50 {pct(.5):.2f} p95 {pct(.95):.2f} "
          f"p99 {pct(.99):.2f} мс | {time.time() - t0:.0f} с", flush=True)
    return r


def train_measure(texts, n):
    gc.collect()
    tracemalloc.start()
    t0 = time.time()
    b = NBase.train(texts, n)
    dt = time.time() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    blob = b.blob()
    t1 = time.time()
    pickle.loads(blob)
    lt = time.time() - t1
    ctxs = " ".join(f"{k + 1}-гр.контекстов {len(b.ctx[k]):,}" for k in b.ctx)
    print(f"  n={n}: обучение {dt:.0f} с, пик памяти обучения {peak / 1e6:.0f} МБ, файл {len(blob) / 1e6:.1f} МБ, "
          f"загрузка {lt:.1f} с | {ctxs}", flush=True)
    return b


def main():
    notes_chars = int(sys.argv[1]) if len(sys.argv) > 1 else 80_000
    tg_chars = int(sys.argv[2]) if len(sys.argv) > 2 else 60_000
    orders = (2, 3, 4)

    print("== A. Заметки: каждая 10-я заметка отложена, обучение только на заметках ==", flush=True)
    train, test = split_corpus()
    text = "\n".join(test)[:notes_chars]
    print(f"тестовый текст {len(text):,} символов", flush=True)
    # контроль: оригинальный Predictor из model.py (n=3) — стенд должен давать то же
    orig = M.Predictor(M.BaseModel.train(train), M.UserModel())
    apply_cfg(orig)
    r = simulate(orig, text)
    print(f"  {'контроль model.py n=3':26} экономия {r['saved'] / r['chars']:6.2%} | показов {r['shown']:5} "
          f"верных {r['accepted']:5}", flush=True)
    bases = {n: train_measure(train, n) for n in orders}
    for n in orders:
        run(f"n={n} (окно {n - 1})", bases[n], n, text)
    run("n=4, k4=12", bases[4], 4, text, kk={3: 12.0})
    run("n=4, k4=3", bases[4], 4, text, kk={3: 3.0})
    run("n=3, +3 слова продления", bases[3], 3, text, max_extra_words=3)
    run("n=4, +3 слова продления", bases[4], 4, text, max_extra_words=3)
    del bases
    gc.collect()

    print("\n== B. Telegram: свежие 10% своих сообщений, обучение: заметки + старые TG (полураспад 2 г., ×4) ==",
          flush=True)
    msgs = telegram_messages(C.TELEGRAM_DIR)
    cut = int(len(msgs) * 0.9)
    tr, te = msgs[:cut], msgs[cut:]
    text = "\n".join(t for _w, t in te)[:tg_chars]
    cfg = C.Config.load()
    corpus = list(iter_texts(C.text_roots(cfg))) + \
        list(iter_telegram(C.TELEGRAM_DIR, cfg.tg_half_life_years, cfg.tg_weight, messages=tr))
    print(f"тестовый текст {len(text):,} символов", flush=True)
    bases = {n: train_measure(corpus, n) for n in orders}
    for n in orders:
        run(f"n={n} (окно {n - 1})", bases[n], n, text)
    run("n=4, k4=12", bases[4], 4, text, kk={3: 12.0})
    run("n=4, k4=3", bases[4], 4, text, kk={3: 3.0})
    run("n=3, +3 слова продления", bases[3], 3, text, max_extra_words=3)
    run("n=4, +3 слова продления", bases[4], 4, text, max_extra_words=3)


if __name__ == "__main__":
    main()
