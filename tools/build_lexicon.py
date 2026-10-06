"""Собрать частотные словари RU/EN для исправления раскладки из wordfreq.

Запуск (нужен только для пересборки, в зависимостях программы его нет):
    pip install wordfreq
    python -X utf8 tools/build_lexicon.py

Результат — halfword/lexicon/{ru,en}.txt.gz: строки «слово<TAB>zipf×100», по убыванию частоты.
Данные wordfreq — CC BY-SA 4.0, см. halfword/lexicon/LICENSE.
"""
import gzip
import re
from pathlib import Path

from wordfreq import top_n_list, zipf_frequency

OUT = Path(__file__).resolve().parent.parent / "halfword" / "lexicon"
LANGS = {
    "ru": (120_000, re.compile(r"[а-яё]+(?:-[а-яё]+)?")),
    "en": (80_000, re.compile(r"[a-z]+(?:'[a-z]+)?")),
}


def main():
    OUT.mkdir(exist_ok=True)
    for lang, (n, ok) in LANGS.items():
        rows = []
        for w in top_n_list(lang, n):
            if ok.fullmatch(w):
                rows.append(f"{w}\t{round(zipf_frequency(w, lang) * 100)}")
        path = OUT / f"{lang}.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write("\n".join(rows) + "\n")
        print(lang, len(rows), f"{path.stat().st_size / 1e6:.2f} МБ")


if __name__ == "__main__":
    main()
