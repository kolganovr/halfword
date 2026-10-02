import hashlib
import tempfile
import unittest
from pathlib import Path

from halfword import setup_llm as S


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src = self.root / "src.bin"
        self.src.write_bytes(b"halfword" * 1000)
        self.sha = hashlib.sha256(self.src.read_bytes()).hexdigest()
        self.url = self.src.as_uri()

    def test_download_verifies_and_renames(self):
        dest = self.root / "out" / "model.gguf"
        seen = []
        S._download(self.url, self.sha, 8000, dest, "model", lambda l, d, t: seen.append(d))
        self.assertEqual(dest.read_bytes(), self.src.read_bytes())
        self.assertFalse(dest.with_name("model.gguf.part").exists())
        self.assertEqual(seen[-1], 8000)

    def test_bad_checksum_removes_part(self):
        dest = self.root / "model.gguf"
        with self.assertRaises(IOError):
            S._download(self.url, "0" * 64, 8000, dest, "model", None)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("model.gguf.part").exists())

    def test_cancel(self):
        with self.assertRaises(InterruptedError):
            S._download(self.url, self.sha, 8000, self.root / "m.gguf", "m", None, cancel=lambda: True)

    def test_unknown_model(self):
        with self.assertRaises(ValueError):
            S.install("nope.gguf")


if __name__ == "__main__":
    unittest.main()
