import tempfile
import unittest
from pathlib import Path

from d3coach import claim_next


class QueueTest(unittest.TestCase):
    def test_claims_images_in_name_order_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as root:
            incoming = Path(root) / "incoming"
            processing = Path(root) / "processing"
            incoming.mkdir()
            (incoming / "002.png").write_bytes(b"second")
            (incoming / "001.JPG").write_bytes(b"first")
            (incoming / "notes.txt").write_text("ignore", encoding="utf-8")

            claimed = claim_next(incoming, processing)

            self.assertEqual(claimed, processing / "001.JPG")
            self.assertEqual(claimed.read_bytes(), b"first")
            self.assertEqual(
                sorted(path.name for path in incoming.iterdir()),
                ["002.png", "notes.txt"],
            )


if __name__ == "__main__":
    unittest.main()
