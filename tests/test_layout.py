"""Раскладка: таблица клавиш, положительные и отрицательные случаи детектора, свойства по словарям, скорость.

Словари грузятся ~1.4 с, поэтому детектор общий на весь файл. Фразы выдуманные.
"""
import time
import unittest
from pathlib import Path

from halfword.layout import EN_KEYS, RU_KEYS, KeyMap, LayoutDetector

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layout"
_DET: LayoutDetector | None = None


def shared() -> LayoutDetector:
    global _DET
    if _DET is None:
        _DET = LayoutDetector()
    return _DET


def top_words(lex, n: int, min_len: int = 3) -> list[str]:
    """Слова словаря по убыванию частоты (lex.sorted — по алфавиту, для выбора не годится)."""
    ranked = sorted(lex.freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _z in ranked if len(w) >= min_len][:n]


class KeyMapTest(unittest.TestCase):
    km = KeyMap()

    def test_tables_equal_length_and_unique(self):
        self.assertEqual(len(EN_KEYS), len(RU_KEYS))
        self.assertEqual(len(set(EN_KEYS)), len(EN_KEYS))
        self.assertEqual(len(set(RU_KEYS)), len(RU_KEYS))

    def test_round_trip_en(self):
        for ch in EN_KEYS:
            with self.subTest(ch=ch):
                self.assertEqual(self.km.convert(self.km.convert(ch, True), False), ch)

    def test_round_trip_ru(self):
        for ch in RU_KEYS:
            with self.subTest(ch=ch):
                self.assertEqual(self.km.convert(self.km.convert(ch, False), True), ch)

    def test_shift_digits_and_punctuation(self):
        pairs = {"@": '"', "#": "№", "$": ";", "^": ":", "&": "?", "/": ".", "?": ","}
        for en, ru in pairs.items():
            with self.subTest(en=en):
                self.assertEqual(self.km.convert(en, True), ru)
                self.assertEqual(self.km.convert(ru, False), en)

    def test_case_and_words(self):
        self.assertEqual(self.km.convert("Ghbdtn", True), "Привет")
        self.assertEqual(self.km.convert("ПРИВЕТ", False), "GHBDTN")
        self.assertEqual(self.km.convert("ghbdtn,", True), "приветб")  # запятая — клавиша «б»
        self.assertEqual(self.km.convert("привет,", False), "ghbdtn?")
        self.assertEqual(self.km.convert("123 -()", True), "123 -()")  # цифры и скобки одинаковы

    def test_unknown_chars_untouched(self):
        self.assertEqual(self.km.convert("a—b «x»", True), "ф—и «ч»")


class PositiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det = shared()

    def check(self, typed: str, expected: str, to_cyr: bool):
        fix = self.det.detect(typed)
        self.assertIsNotNone(fix, typed)
        self.assertEqual(fix.insert, expected)
        self.assertEqual(fix.erase, len(typed))
        self.assertEqual(fix.original, typed)
        self.assertEqual(fix.to_cyr, to_cyr)
        self.assertTrue(fix.closed)

    def test_en_to_ru_word(self):
        self.check("ghbdtn ", "привет ", True)

    def test_ru_to_en_word(self):
        self.check("ащк ", "for ", False)

    def test_chain_with_short_words(self):
        self.check("z nt,z k.,k. ", "я тебя люблю ", True)

    def test_chain_with_punctuation(self):
        self.check("Ghbdtn, rfr ltkf? ", "Привет, как дела, ", True)  # «?» на латинской клавише — «,» в русской

    def test_caps(self):
        self.check("GHBDTN ", "ПРИВЕТ ", True)

    def test_letters_from_punctuation_keys(self):
        self.check("cgfcb,j ", "спасибо ", True)  # «,» — клавиша «б»

    def test_trailing_punctuation_after_word(self):
        fix = self.det.detect("ghbdtn? ")
        self.assertIsNotNone(fix)
        self.assertEqual(fix.insert, "привет, ")

    def test_text_before_chain_kept(self):
        fix = self.det.detect("мы уже ghbdtn ")
        self.assertIsNotNone(fix)
        self.assertEqual((fix.erase, fix.insert), (len("ghbdtn "), "привет "))

    def test_auto_for_confident_word(self):
        self.assertTrue(self.det.detect("ghbdtn ").auto)
        self.assertTrue(self.det.detect("ащк ").auto)

    def test_convert_helpers(self):
        self.assertEqual(self.det.convert_text("ghbdtn rfr ltkf"), ("привет как дела", True))
        self.assertEqual(self.det.convert_text("привет"), ("ghbdtn", False))
        fix = self.det.convert_last("мы ghbdtn")
        self.assertEqual((fix.erase, fix.insert), (len("ghbdtn"), "привет"))
        self.assertTrue(self.det.is_wrong_layout("ghbdtn"))
        self.assertFalse(self.det.is_wrong_layout("привет"))


class NegativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det = shared()

    def test_silent(self):
        cases = [
            "сделал commit ", "iPhone ", "privet ", "https://example.com/x ", "name@example.org ",
            "C:\\Users\\test ", "h264 ", "ok ", "привет ", "the ", "lf ", "vs ", "lf vs ", "привет, как дела ",
            "I think so ", "check the README ", "", " ", "\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(self.det.detect(text))

    def test_never_word(self):
        det = LayoutDetector(self.det.ru, self.det.en, never=["ghbdtn"])
        self.assertIsNone(det.detect("ghbdtn "))
        self.assertIsNone(det.detect("Ghbdtn "))
        self.assertIsNotNone(self.det.detect("ghbdtn "))

    def test_no_second_fix_after_correction(self):
        self.assertIsNone(self.det.detect("привет как дела "))

    def test_english_word_with_final_period(self):
        # «.» — клавиша «ю», а «ше» + «ю» = «шею»: точка после английского слова — обычная пунктуация
        self.assertIsNone(self.det.detect("you can do it. "))

    def test_english_word_with_comma(self):
        self.assertIsNone(self.det.detect("what is it, "))  # «it,» → «ше,» (auto)

    def test_latin_word_with_colon_in_russian_layout(self):
        # «commit:» в русской раскладке — «сщььшеЖ»: заглавная «Ж» — это Shift+;, а не смешанный регистр
        fix = self.det.detect("сщььшеЖ ")
        self.assertIsNotNone(fix)
        self.assertEqual(fix.insert, "commit: ")


class OpenWordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det = shared()

    def test_open_word_suggests_but_never_auto(self):
        fix = self.det.detect("ghbd")
        self.assertIsNotNone(fix)
        self.assertFalse(fix.closed)
        self.assertFalse(fix.auto)
        self.assertTrue(fix.insert.startswith("при"))

    def test_open_own_prefix_silent(self):
        self.assertIsNone(self.det.detect("hell"))
        self.assertIsNone(self.det.detect("прив"))


class DictionaryPropertiesTest(unittest.TestCase):
    """Для топ-5000 слов словарей (длина ≥3): в своей раскладке молчим, в чужой — исправляем в это же слово."""

    TOP = 5000
    # доля верных исправлений в чужой раскладке на момент написания: ru 99.82%, en 99.70%; граница — с запасом 0.5 п.п.
    # английские 3–4 буквы в русской раскладке («her» = «рук») в одиночку неоднозначны —
    # исправляются только рядом с уверенным соседом
    FOREIGN_MIN = {"ru": 0.993, "en": 0.98}

    @classmethod
    def setUpClass(cls):
        cls.det = shared()
        cls.km = KeyMap()

    def own_layout_fires(self, lex) -> list[str]:
        return [w for w in top_words(lex, self.TOP) if self.det.detect(w + " ") is not None]

    def foreign_misses(self, lex, to_cyr: bool) -> tuple[int, list[str]]:
        words = top_words(lex, self.TOP)
        miss = []
        for w in words:
            fix = self.det.detect(self.km.convert(w, to_cyr) + " ")
            if fix is None or fix.insert != w + " ":
                miss.append(w)
        return len(words), miss

    def test_ru_own_layout_silent(self):
        fires = self.own_layout_fires(self.det.ru)
        self.assertEqual(fires, [], f"{len(fires)} слов, примеры: {fires[:20]}")

    def test_en_own_layout_silent(self):
        fires = self.own_layout_fires(self.det.en)
        self.assertEqual(fires, [], f"{len(fires)} слов, примеры: {fires[:20]}")

    def test_ru_words_typed_in_english_layout(self):
        n, miss = self.foreign_misses(self.det.ru, to_cyr=False)
        share = 1 - len(miss) / n
        self.assertGreaterEqual(share, self.FOREIGN_MIN["ru"], f"не исправлено {len(miss)}: {miss[:20]}")

    def test_en_words_typed_in_russian_layout(self):
        n, miss = self.foreign_misses(self.det.en, to_cyr=True)
        share = 1 - len(miss) / n
        self.assertGreaterEqual(share, self.FOREIGN_MIN["en"], f"не исправлено {len(miss)}: {miss[:20]}")


class SpeedTest(unittest.TestCase):
    CALLS = 2000

    def test_p95_under_2ms(self):
        shared()
        det = LayoutDetector(shared().ru, shared().en)  # свежий кэш слов: без поблажек
        km = KeyMap()
        buffers = []
        for fn in ("clean_ru.txt", "clean_en.txt", "hard_ru.txt", "hard_en.txt"):
            for line in (FIXTURES / fn).read_text(encoding="utf-8").splitlines():
                for typed in (line, km.convert(line, any("a" <= c.lower() <= "z" for c in line))):
                    words = typed.split()
                    buffers += [" ".join(words[:i]) + " " for i in range(1, len(words) + 1)]
        self.assertGreaterEqual(len(buffers), self.CALLS)
        step = len(buffers) / self.CALLS
        sample = [buffers[int(i * step)] for i in range(self.CALLS)]
        times = []
        for text in sample:
            t0 = time.perf_counter()
            det.detect(text)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        p95 = times[int(0.95 * len(times))]
        self.assertLess(p95, 2.0, f"p95 = {p95:.2f} мс")


class EvaluateStandTest(unittest.TestCase):
    """Стенд evaluate_layout: разметка искажений и симуляция набора на заглушке детектора."""

    def test_variants_deterministic_and_marked(self):
        from halfword.evaluate_layout import make_variants
        msg = "привет как дела у тебя сегодня, не забыл про commit?"
        a = make_variants(msg, "clean", seed=7)
        b = make_variants(msg, "clean", seed=7)
        self.assertEqual([(v.kind, [t.typed for t in v.toks]) for v in a], [(v.kind, [t.typed for t in v.toks]) for v in b])
        self.assertEqual({v.kind for v in a}, {"clean", "word", "seg", "all", "head"})
        for v in a:
            self.assertEqual(" ".join(t.correct for t in v.toks), msg)
            for t in v.toks:
                self.assertEqual(len(t.typed), len(t.correct))
                if t.dist:
                    self.assertNotEqual(t.typed, t.correct)
                    self.assertEqual(KeyMap().convert(t.typed, t.script == "cyr"), t.correct)
                else:
                    self.assertEqual(t.typed, t.correct)
            if v.kind in ("seg", "all", "head"):
                self.assertGreaterEqual(v.nspan, 2)

    def test_not_distorted_tokens(self):
        from halfword.evaluate_layout import classify
        for tok in ("h264", "https://example.com", "iPhone", "a_b", "--help", "42", "—", "C:\\x"):
            self.assertIsNone(classify(tok)[0], tok)
        self.assertEqual(classify("«привет,»"), ("cyr", 6))
        self.assertEqual(classify("don't"), ("lat", 4))

    def test_simulation_with_oracle_engine(self):
        from halfword.evaluate_layout import Fix, Stats, make_variants, simulate

        class Always:
            delim, has_auto = True, True

            def __init__(self, km):
                self.km = km

            def fix(self, text, fold):
                last = text.rstrip(" ").rsplit(" ", 1)[-1]
                if last and all(c in "abcdefghijklmnopqrstuvwxyz;,.[]'`" for c in last) and last != "commit":
                    return Fix(len(last) + 1, self.km.convert(last, True) + " ", True)
                return None

        km = KeyMap()
        st = Stats()
        variants = [v for v in make_variants("привет как дела у тебя", "clean", seed=3) if v.kind != "clean"]
        for v in variants:
            simulate(v, Always(km), "auto", st)
        self.assertEqual(sum(r[0] for r in st.rec.values()), sum(r[2] for r in st.rec.values()))  # всё найдено верно
        self.assertEqual(st.chain[0], st.chain[1])
        self.assertEqual(st.fp_dist + sum(st.fp.values()), 0)
        # ложное: на чистом сообщении
        st2 = Stats()
        clean = [v for v in make_variants("commit now", "clean", seed=3) if v.kind == "clean"][0]
        simulate(clean, Always(km), "auto", st2)
        self.assertEqual(st2.fp["clean"], 1)
        self.assertEqual(st2.words["clean"], 2)

    def test_auto_mode_ignores_non_auto_fixes(self):
        from halfword.evaluate_layout import Fix, Stats, make_variants, simulate

        class Soft:
            delim, has_auto = True, True

            def fix(self, text, fold):
                last = text.rstrip(" ").rsplit(" ", 1)[-1]
                if last == "ghbdtn":
                    return Fix(len(last) + 1, "привет ", False)
                return None

        v = [x for x in make_variants("привет", "clean", seed=1) if x.kind == "word"][0]
        st = Stats()
        simulate(v, Soft(), "auto", st)
        self.assertEqual(st.rec[("en→ru", "5+")], [1, 0, 0])
        st = Stats()
        simulate(v, Soft(), "suggest", st)
        self.assertEqual(st.rec[("en→ru", "5+")], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
