import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from d3coach import (
    Item,
    Layout,
    RawItem,
    Recognition,
    analyze,
    claim_next,
    crop_panel,
    extract_item,
    locate,
    normalize_item,
    process_next,
    watch,
)


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

    @patch("d3coach.analyze")
    def test_processes_one_screenshot_and_saves_json(self, analyze_mock):
        analyze_mock.return_value = Recognition(
            screen_type="other",
            panels=[],
            items=[],
            stats={},
            materials={},
            confidence=1.0,
        )
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "screenshots"
            results = Path(root) / "results"
            (queue / "incoming").mkdir(parents=True)
            (queue / "incoming" / "001.png").write_bytes(b"image")

            processed = process_next(queue, results)

            self.assertTrue(processed)
            self.assertTrue((queue / "done" / "001.png").exists())
            self.assertEqual(
                json.loads((results / "001.json").read_text(encoding="utf-8"))[
                    "screen_type"
                ],
                "other",
            )

    @patch("d3coach.analyze", side_effect=RuntimeError("model failed"))
    def test_moves_failed_screenshots_aside_and_continues(self, _):
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "screenshots"
            (queue / "incoming").mkdir(parents=True)
            (queue / "incoming" / "001.png").write_bytes(b"image")

            processed = process_next(queue, Path(root) / "results")

            self.assertTrue(processed)
            self.assertTrue((queue / "failed" / "001.png").exists())
            self.assertEqual(
                (queue / "failed" / "001.error.txt").read_text(encoding="utf-8"),
                "model failed",
            )

    @patch("d3coach.sleep")
    @patch("d3coach.process_next", side_effect=[False, KeyboardInterrupt])
    def test_watcher_waits_only_when_the_queue_is_empty(
        self, process_mock, sleep_mock
    ):
        with self.assertRaises(KeyboardInterrupt):
            watch(Path("screenshots"), Path("results"), poll_seconds=0.5)

        self.assertEqual(process_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.5)


class CropTest(unittest.TestCase):
    def test_crops_normalized_panel_coordinates(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.png"
            target = Path(root) / "crops" / "panel.png"
            Image.new("RGB", (200, 100), "black").save(source)

            crop_panel(source, [250, 200, 750, 800], target, padding=0)

            with Image.open(target) as crop:
                self.assertEqual(crop.size, (100, 60))


class AnalyzeTest(unittest.TestCase):
    @patch("d3coach.unload")
    @patch("d3coach.extract_item")
    @patch("d3coach.locate")
    def test_analyzes_each_item_panel_and_unloads_the_model(
        self, locate_mock, extract_mock, unload_mock
    ):
        locate_mock.return_value = Layout(
            screen_type="single_item",
            panels=[
                {
                    "kind": "item",
                    "role": "candidate",
                    "owner": "player",
                    "title": "Höllenläufer",
                    "box_1000": [100, 100, 900, 900],
                }
            ],
            confidence=0.9,
        )
        extract_mock.return_value = Item(
            role="candidate",
            owner="player",
            name="Höllenläufer",
            quality="set",
            slot="Füße",
            armor=549,
            dps=None,
            primary_affixes=[],
            secondary_affixes=[],
            set_name="Unheilige Essenz",
            legendary_power=None,
        )

        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (100, 100), "black").save(image)

            result = analyze(image)

        self.assertEqual(result.items[0].name, "Höllenläufer")
        self.assertEqual(locate_mock.call_args.kwargs["keep_alive"], "5m")
        self.assertEqual(extract_mock.call_args.kwargs["keep_alive"], "5m")
        unload_mock.assert_called_once()

    def test_script_entrypoint_reaches_the_crop_pipeline(self):
        def open_request(request, timeout):
            payload = json.loads(request.data)
            if request.full_url.endswith("/generate"):
                content = {}
            elif payload["format"]["title"] == "Layout":
                content = {
                    "message": {
                        "content": json.dumps(
                            {
                                "screen_type": "single_item",
                                "panels": [
                                    {
                                        "kind": "item",
                                        "role": "candidate",
                                        "owner": "player",
                                        "title": "Test",
                                        "box_1000": [100, 100, 900, 900],
                                    }
                                ],
                                "confidence": 1.0,
                            }
                        )
                    }
                }
            else:
                content = {
                    "message": {
                        "content": json.dumps(
                            {
                                "role": "candidate",
                                "owner": "player",
                                "name": "Test",
                                "quality_text": "Set: Stiefel",
                                "slot": "Füße",
                                "armor": 1,
                                "dps": None,
                                "primary_affixes": [],
                                "secondary_affixes": [],
                                "set_name": "Testset",
                                "legendary_power": None,
                            }
                        )
                    }
                }

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def read(self):
                    return json.dumps(content).encode()

            return Response()

        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (100, 100), "black").save(image)
            with (
                patch("urllib.request.urlopen", new=open_request),
                patch.object(sys, "argv", ["d3coach.py", "analyze", str(image)]),
                patch("builtins.print"),
            ):
                runpy.run_path("d3coach.py", run_name="__main__")


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

    def test_extracts_one_item_crop_with_fixed_role_and_owner(self):
        item_content = {
            "role": "equipped",
            "owner": "player",
            "name": "HÖLLENLÄUFER",
            "quality_text": "Uraltes Set: Stiefel",
            "slot": "Füße",
            "armor": 538,
            "dps": 0.0,
            "primary_affixes": ["+630 Geschicklichkeit [550 - 650]"],
            "secondary_affixes": ["+198 Kältewiderstand [176 - 210]"],
            "set_name": "Unheilige Essenz",
            "legendary_power": None,
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self):
                return json.dumps(
                    {"message": {"content": json.dumps(item_content)}}
                ).encode()

        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return Response()

        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "item.png"
            image.write_bytes(b"image bytes")

            item = extract_item(
                image,
                role="equipped",
                owner="player",
                opener=open_request,
            )

        payload = json.loads(requests[0].data)
        self.assertEqual(payload["format"], RawItem.model_json_schema())
        self.assertIn("role=equipped", payload["messages"][0]["content"])
        self.assertEqual(item.name, "HÖLLENLÄUFER")
        self.assertEqual(item.quality, "ancient_set")
        self.assertIsNone(item.dps)

    def test_normalizes_visible_german_quality_text(self):
        raw = RawItem(
            role="candidate",
            owner="player",
            name="Höllenläufer",
            quality_text="",
            slot="Füße",
            armor=549,
            dps=None,
            primary_affixes=[],
            secondary_affixes=[],
            set_name="Unheilige Essenz",
            legendary_power=None,
        )

        item = normalize_item(raw, role="candidate", owner="player")

        self.assertEqual(item.quality, "set")

    def test_recovers_a_misplaced_ancient_set_label(self):
        raw = RawItem(
            role="equipped",
            owner="player",
            name="Höllenläufer",
            quality_text="",
            slot="Füße",
            armor=538,
            dps=0.0,
            primary_affixes=[],
            secondary_affixes=[],
            set_name="Uraltes Set: Stiefel",
            legendary_power="Eure Hasserzeuger erzeugen zusätzlich 2 Hass.",
        )

        item = normalize_item(raw, role="equipped", owner="player")

        self.assertEqual(item.quality, "ancient_set")
        self.assertIsNone(item.set_name)
        self.assertIsNone(item.legendary_power)


if __name__ == "__main__":
    unittest.main()
