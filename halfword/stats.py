"""Статистика использования по дням: показы, принятия, сэкономленные нажатия и время.

Время = сэкономленные нажатия × средняя пауза между нажатиями (меряется на лету,
паузы длиннее 1.5 с не считаются — это раздумья, а не набор).
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

FIELDS = ("typed", "shown", "accepted", "inserted", "saved", "type_ms", "type_n", "llm_shown", "llm_accepted",
          "llm_saved", "long_shown", "long_accepted", "long_saved", "word_accepts")
MAX_GAP_MS = 1500


class Stats:
    def __init__(self, path: Path):
        self.path = path
        self.days: dict[str, dict] = {}
        if path.exists():
            try:
                self.days = json.loads(path.read_text(encoding="utf-8")).get("days", {})
            except (OSError, ValueError):
                pass
        self._last_key = 0.0
        self._last_shown = None
        self.dirty = False

    def _today(self) -> dict:
        d = self.days.setdefault(date.today().isoformat(), {})
        for f in FIELDS:
            d.setdefault(f, 0)
        d.setdefault("no_caret", {})
        return d

    # --- события ---
    def typed(self):
        now = time.perf_counter()
        d = self._today()
        d["typed"] += 1
        gap = (now - self._last_key) * 1000
        if 0 < gap < MAX_GAP_MS:
            d["type_ms"] += int(gap)
            d["type_n"] += 1
        self._last_key = now
        self.dirty = True

    def break_typing(self):
        self._last_key = 0.0

    def shown(self, insert: str, llm: bool = False, long: bool = False):
        # считаем разные подсказки, а не каждую перерисовку одной и той же (и не каждый шаг роста в паузе)
        last = self._last_shown
        if not last or not (last.endswith(insert) or insert.startswith(last)):
            d = self._today()
            d["shown"] += 1
            if llm:
                d["llm_shown"] += 1
            if long:
                d["long_shown"] += 1
            self.dirty = True
        self._last_shown = insert

    def accepted(self, inserted: str, llm: bool = False, long: bool = False, new: bool = True,
                 word: bool = False):
        """new=False — продолжение уже принятой подсказки (Ctrl+→ по словам): принятием не считается."""
        d = self._today()
        saved = max(0, len(inserted) - 1)  # минус само нажатие Tab / Ctrl+→
        d["accepted"] += new
        d["inserted"] += len(inserted)
        d["saved"] += saved
        d["word_accepts"] += word
        if llm:
            d["llm_accepted"] += new
            d["llm_saved"] += saved
        if long:
            d["long_accepted"] += new
            d["long_saved"] += saved
        self._last_shown = None
        self._last_key = time.perf_counter()
        self.dirty = True

    def no_caret(self, exe: str):
        nc = self._today()["no_caret"]
        nc[exe] = nc.get(exe, 0) + 1
        self.dirty = True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"days": self.days}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False

    # --- отчёт ---
    def _sum(self, days: int | None) -> dict:
        since = (date.today() - timedelta(days=days - 1)).isoformat() if days else ""
        tot = {f: 0 for f in FIELDS}
        for day, d in self.days.items():
            if day >= since:
                for f in FIELDS:
                    tot[f] += d.get(f, 0)
        return tot

    # --- для веб-панели: те же числа, что в report, но словарём ---
    def summary(self, days: int | None) -> dict:
        """Итоги за days дней (None — всё время): показано, принято, сэкономлено нажатий и времени."""
        t = self._sum(days)
        measured = bool(t["type_n"] and t["type_ms"] > 0)
        ms = t["type_ms"] / t["type_n"] if measured else 200.0
        total_keys = t["typed"] + t["saved"]
        return dict(t, measured=measured, ms_per_key=round(ms, 1), secs=round(t["saved"] * ms / 1000, 1),
                    share=t["saved"] / total_keys if total_keys else 0.0,
                    accept_rate=t["accepted"] / t["shown"] if t["shown"] else 0.0,
                    cpm=round(60000 / ms) if measured else 0)

    def daily(self, days: int) -> list[dict]:
        """По дню на каждый из последних days дней (старые первыми, пустые дни — нули): для графика."""
        out = []
        for i in range(days - 1, -1, -1):
            day = (date.today() - timedelta(days=i)).isoformat()
            d = self.days.get(day, {})
            out.append({"day": day, **{f: d.get(f, 0) for f in ("typed", "shown", "accepted", "saved")}})
        return out

    def no_caret_apps(self, days: int) -> dict[str, int]:
        """Программы, где не нашёлся курсор, за days дней: exe → сколько раз."""
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        tot: dict[str, int] = {}
        for day, d in self.days.items():
            if day >= since:
                for exe, n in d.get("no_caret", {}).items():
                    tot[exe] = tot.get(exe, 0) + n
        return tot

    def report(self) -> str:
        lines = []
        for title, days in (("Сегодня", 1), ("7 дней", 7), ("Всего", None)):
            t = self._sum(days)
            measured = t["type_n"] and t["type_ms"] > 0
            ms = t["type_ms"] / t["type_n"] if measured else 200.0  # 200 мс/знак, пока не измерено
            secs = t["saved"] * ms / 1000
            total_keys = t["typed"] + t["saved"]
            share = t["saved"] / total_keys if total_keys else 0
            acc = t["accepted"] / t["shown"] if t["shown"] else 0
            cpm = 60000 / ms if measured else 0
            lines.append(
                f"{title}:\n"
                f"  принято подсказок: {t['accepted']} из {t['shown']} показанных ({acc:.0%})\n"
                f"  сэкономлено нажатий: {t['saved']} ({share:.1%} от всего набора)\n"
                f"  сэкономлено времени: {_fmt(secs)}"
                + (f"  (скорость набора ~{cpm:.0f} зн/мин)" if cpm else "")
                + (f"\n  из них LLM ✦: принято {t['llm_accepted']} из {t['llm_shown']}, "
                   f"сэкономлено {t['llm_saved']} нажатий" if t["llm_shown"] else "")
                + (f"\n  из них в паузе ✦✦: принято {t['long_accepted']} из {t['long_shown']}, "
                   f"сэкономлено {t['long_saved']} нажатий, по словам (Ctrl+→) — {t['word_accepts']} раз"
                   if t["long_shown"] else "")
            )
        nc = self.days.get(date.today().isoformat(), {}).get("no_caret", {})
        if nc:
            top = sorted(nc.items(), key=lambda x: -x[1])[:5]
            lines.append("Не нашёл курсор сегодня: " + ", ".join(f"{k} ×{v}" for k, v in top))
        return "\n\n".join(lines)


def _fmt(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f} с"
    if secs < 3600:
        return f"{secs / 60:.1f} мин"
    return f"{secs / 3600:.1f} ч"
