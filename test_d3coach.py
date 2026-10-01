import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from d3coach import Layout, Recognition, claim_next, locate


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


class RecognitionTest(unittest.TestCase):
    def test_accepts_a_valid_item_comparison(self):
        recognition = Recognition.model_validate(
            {
                "screen_type": "item_comparison",
                "panels": [
                    {
                        "kind": "item",
                        "role": "candidate",
                        "owner": "player",
                        "title": "HÖLLENLÄUFER",
                        "box_1000": [150, 20, 400, 930],
                    }
                ],
                "items": [
                    {
                        "role": "candidate",
                        "owner": "player",
                        "name": "HÖLLENLÄUFER",
                        "quality": "set",
                        "slot": "Füße",
                        "armor": 549,
                        "dps": None,
                        "primary_affixes": ["+495 Geschicklichkeit"],
                        "secondary_affixes": ["+160 Blitzwiderstand"],
                        "set_name": "Unheilige Essenz",
                        "legendary_power": None,
                    }
                ],
                "stats": {},
                "materials": {},
                "confidence": 0.95,
            }
        )

        self.assertEqual(recognition.items[0].role, "candidate")

    def test_rejects_ambiguous_roles_and_inverted_boxes(self):
        invalid = {
            "screen_type": "item_comparison",
            "panels": [
                {
                    "kind": "item",
                    "role": "candidate",
                    "owner": "player",
                    "title": "HÖLLENLÄUFER",
                    "box_1000": [400, 20, 150, 930],
                }
            ],
            "items": [
                {
                    "role": "candidate|equipped",
                    "owner": "player",
                    "name": "HÖLLENLÄUFER",
                    "quality": "set",
                    "slot": "Füße",
                    "armor": 549,
                    "dps": None,
                    "primary_affixes": [],
                    "secondary_affixes": [],
                    "set_name": None,
                    "legendary_power": None,
                }
            ],
            "stats": {},
            "materials": {},
            "confidence": 0.95,
        }

        with self.assertRaises(ValidationError):
            Recognition.model_validate(invalid)

    def test_sends_the_layout_schema_to_local_ollama(self):
        response_content = {
            "screen_type": "single_item",
            "panels": [
                {
                    "kind": "item",
                    "role": "candidate",
                    "owner": "player",
                    "title": "HÖLLENLÄUFER",
                    "box_1000": [150, 20, 400, 930],
                }
            ],
            "confidence": 0.8,
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self):
                return json.dumps(
                    {"message": {"content": json.dumps(response_content)}}
                ).encode()

        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return Response()

        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "item.png"
            image.write_bytes(b"image bytes")

            layout = locate(image, opener=open_request)

        payload = json.loads(requests[0][0].data)
        self.assertEqual(requests[0][0].full_url, "http://localhost:11434/api/chat")
        self.assertEqual(payload["model"], "qwen3-vl:8b-instruct")
        self.assertEqual(payload["format"], Layout.model_json_schema())
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(payload["options"]["num_predict"], 512)
        self.assertEqual(layout.panels[0].role, "candidate")


if __name__ == "__main__":
    unittest.main()
