import time
import unittest
from types import SimpleNamespace

from halfword import app as A
from halfword.config import Config
from halfword.llm import build_insert, lead_grammar, norm_prob, prompt_window, split_lead


class LLMHelpersTest(unittest.TestCase):
    def test_split_lead(self):
        self.assertEqual(split_lead("Привет, как де", "де"), ("Привет, как", " де"))
        self.assertEqual(split_lead("Скинь, пожалуйста, ", ""), ("Скинь, пожалуйста,", " "))
        self.assertEqual(lead_grammar(' "x'), r'root ::= " \"x" [^\n]*')

    def test_build_insert(self):
        self.assertEqual(build_insert([(" дела", .9), ("?", .8), (" Я", .5)], " де", .3, 4)[0], "ла?")
        self.assertEqual(build_insert([(" ссылку", .5), (" на", .9), (" ста", .3)], " ", .3, 4)[0], "ссылку на")
        self.assertIsNone(build_insert([(" де", .9), (" я", .9)], " де", .3, 4))  # слово уже закончено
        self.assertIsNone(build_insert([(" дела", .2)], " де", .3, 4))            # ниже порога
        self.assertEqual(build_insert([(" Бат", .8), ("уми", .9)], " Бат", .3, 4, True)[0], "уми")  # конец генерации

    def test_norm_prob(self):
        self.assertAlmostEqual(norm_prob(.02, [(" мне", .07), (" д", .01), (" дела", .02)], " де"), 2 / 3)
        self.assertLess(norm_prob(1e-4, [(" мне", .5)], " де"), .01)  # совместимых почти нет — не уверенность
        self.assertEqual(norm_prob(.3, [], ""), .3)

    def test_prompt_window_stable_start(self):
        t = "слово " * 120
        w1, st = prompt_window(t, None)
        w2, _ = prompt_window(t + "ещё", st)
        self.assertTrue(w2.startswith(w1[:40]))


class ManageLLMTest(unittest.TestCase):
    def _fake(self, on_ac, last_key_ago):
        calls = []
        llm = SimpleNamespace(running=False, available=True,
                              start=lambda: calls.append("start"), stop=lambda r="": calls.append(("stop", r)))
        me = SimpleNamespace(cfg=Config(), shared=SimpleNamespace(enabled=True), llm=llm, tray=None,
                             power_checked=0.0, on_ac=True, last_key=time.time() - last_key_ago, llm_res=None)
        A.on_ac_power = lambda: on_ac
        return me, calls

    def test_starts_on_ac_when_typing(self):
        me, calls = self._fake(True, 1)
        A.App._manage_llm(me)
        self.assertEqual(calls, ["start"])

    def test_stops_on_battery(self):
        me, calls = self._fake(False, 1)
        me.llm.running = True
        A.App._manage_llm(me)
        self.assertEqual(calls, [("stop", "батарея")])

    def test_stops_when_idle(self):
        me, calls = self._fake(True, 16 * 60)
        me.llm.running = True
        A.App._manage_llm(me)
        self.assertEqual(calls, [("stop", "простой")])


if __name__ == "__main__":
    unittest.main()
