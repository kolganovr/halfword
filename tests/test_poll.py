"""Главный цикл: шаг 8 мс, пока печатают, до 100 мс в простое; событие из потока будит цикл сразу."""
import threading
import time
import unittest
from types import SimpleNamespace

from halfword import app as A


def stub_app(root):
    me = A.App.__new__(A.App)
    me._wake = threading.Event()
    me.events = A.WakeQueue(me._wake)
    me.cmds = A.WakeQueue(me._wake)
    me.root = root
    me.shared = SimpleNamespace(handling_seq=0, gated=False, gate_seq=0)
    me.llm = SimpleNamespace(running=True, cancel=lambda: None, ready=False)
    me.llm_was_ready = False
    me.power_checked = me.last_save = 1e18
    me.tray = None
    me.exe = ""
    me._tick = lambda: None
    me._show_now = lambda: None
    me.handled = []
    me._handle = lambda kind, *a: me.handled.append(kind) or False
    me.cfg = SimpleNamespace(llm_delay_ms=0, llm_pause_ms=0)
    return me


class FakeRoot:
    def __init__(self):
        self.after_calls = []

    def after(self, ms, fn, *a):
        self.after_calls.append(ms)
        return f"job{len(self.after_calls)}"

    def after_cancel(self, job):
        pass


class ScheduleTest(unittest.TestCase):
    def test_poll_delay_steps(self):
        self.assertEqual([A.poll_delay(s) for s in (0, 1.9, 2.5, 4.9, 6, 14, 15, 600)],
                         [8, 8, 25, 25, 50, 50, 100, 100])

    def test_step_follows_activity(self):
        root = FakeRoot()
        me = stub_app(root)
        me.events.put((1, "char", "a"))
        me._poll()
        self.assertEqual(root.after_calls[-1], 8)        # только что нажали
        me._activity = time.monotonic() - 3
        me._poll()
        self.assertEqual(root.after_calls[-1], 25)
        me._activity = time.monotonic() - 60
        me._poll()
        self.assertEqual(root.after_calls[-1], 100)      # простой
        me.cmds.put(lambda: None)                         # команда панели/потока — не нажатие: шаг не сбрасывает
        me._poll()
        self.assertEqual(root.after_calls[-1], 100)
        me.events.put((2, "char", "b"))
        me._poll()
        self.assertEqual((root.after_calls[-1], me.handled), (8, ["char", "char"]))

    def test_queue_put_sets_wake_flag(self):
        ev = threading.Event()
        q = A.WakeQueue(ev)
        q.put(1)
        self.assertTrue(ev.is_set())
        self.assertEqual(q.get_nowait(), 1)


class WakeTkTest(unittest.TestCase):
    """С настоящим tk (корень скрыт): цикл спит 100 мс, событие из чужого потока обрабатывается сразу."""

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"tk недоступен: {e}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_event_from_thread_wakes_idle_loop(self):
        me = stub_app(self.root)
        me._activity = time.monotonic() - 600
        self.root.bind("<<HalfwordWake>>", me._wake_poll)
        me._poll()
        self.assertEqual(me._poll_ms, 100)
        me._poll_job = self.root.after(10_000, me._poll)  # следующий круг «нескоро»: успеть может только будильник
        threading.Thread(target=me._waker, daemon=True).start()
        t0 = time.monotonic()
        threading.Timer(0.05, lambda: me.events.put((1, "char", "x"))).start()
        while not me.handled and time.monotonic() - t0 < 2:
            self.root.update()
            time.sleep(0.001)
        self.assertEqual(me.handled, ["char"])
        self.assertLess(time.monotonic() - t0, 0.05 + 0.08)  # без будильника ждали бы 10 с
        self.assertEqual(me._poll_ms, 8)


if __name__ == "__main__":
    unittest.main()
