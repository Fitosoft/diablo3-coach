import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
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
    analyze_many,
    claim_next,
    crop_panel,
    extract_item,
    extract_materials,
    extract_stats,
    locate,
    locate_with_fallback,
    normalize_item,
    process_next,
    watch,
)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.application_print = self.enterContext(patch("d3coach.print", create=True))

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
            screen_type="materials",
            panels=[],
            items=[],
            stats={},
            materials={"Staub": 1},
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
                "materials",
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
            self.application_print.assert_called_once_with(
                "ERROR 001.png: model failed", flush=True
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


class TestOutputTest(unittest.TestCase):
    def test_expected_failure_test_does_not_leak_application_output(self):
        output = StringIO()
        result = unittest.TestResult()
        with redirect_stdout(output):
            QueueTest("test_moves_failed_screenshots_aside_and_continues").run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(output.getvalue(), "")


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
    @patch("d3coach.locate")
    def test_skips_middle_crop_when_full_image_has_panels(self, locate_mock):
        expected = Layout(
            screen_type="single_item", confidence=1,
            panels=[{
                "kind": "item", "role": "equipped", "owner": "player",
                "title": "Ring", "box_1000": [600, 0, 900, 700],
            }],
        )
        locate_mock.return_value = expected

        self.assertIs(locate_with_fallback(Path("unused.png")), expected)
        locate_mock.assert_called_once()

    @patch("d3coach.locate")
    def test_retries_empty_layout_on_middle_crop_and_maps_boxes_back(self, locate_mock):
        locate_mock.side_effect = [
            Layout(screen_type="other", panels=[], confidence=1),
            Layout(
                screen_type="single_item", confidence=1,
                panels=[{
                    "kind": "item", "role": "candidate", "owner": "player",
                    "title": "Buriza", "box_1000": [100, 100, 600, 900],
                }],
            ),
        ]
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440), "black").save(image)

            result = locate_with_fallback(image)

        self.assertEqual(locate_mock.call_count, 2)
        self.assertEqual(result.panels[0].box_1000, [222, 100, 554, 900])

    @patch("d3coach.locate")
    def test_rejects_panel_cut_off_by_middle_crop(self, locate_mock):
        locate_mock.side_effect = [
            Layout(screen_type="other", panels=[], confidence=1),
            Layout(
                screen_type="single_item", confidence=1,
                panels=[{
                    "kind": "item", "role": "candidate", "owner": "player",
                    "title": "Ring", "box_1000": [850, 10, 1000, 620],
                }],
            ),
            Layout(screen_type="other", panels=[], confidence=1),
            Layout(screen_type="other", panels=[], confidence=1),
        ]
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440), "black").save(image)

            result = locate_with_fallback(image)

        self.assertEqual(result.panels, [])

    @patch("d3coach.locate")
    def test_half_fallback_keeps_only_complete_candidate_and_remaps(self, locate_mock):
        empty = Layout(screen_type="other", panels=[], confidence=1)
        locate_mock.side_effect = [empty, empty, empty, Layout(
            screen_type="item_comparison", confidence=1, panels=[
                {"kind": "item", "role": "candidate", "owner": "player",
                 "title": "Sunwuko", "box_1000": [80, 0, 600, 900]},
                {"kind": "item", "role": "equipped", "owner": "player",
                 "title": "Vergleich", "box_1000": [0, 0, 50, 900]},
            ])]
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440), "black").save(image)
            result = locate_with_fallback(image)
        self.assertEqual(result.screen_type, "single_item")
        self.assertEqual(result.panels[0].box_1000, [525, 0, 793, 900])
        self.assertEqual(len(result.panels), 1)

    @patch("d3coach.unload")
    @patch("d3coach.analyze")
    def test_merges_two_scroll_views_serially_with_later_values_winning(
        self, analyze_mock, unload_mock
    ):
        analyze_mock.side_effect = [
            Recognition(
                screen_type="materials", panels=[], items=[], stats={},
                materials={"Arkanstaub": 10, "Atem des Todes": 4},
                confidence=1, source_images=["first.png"],
            ),
            Recognition(
                screen_type="materials", panels=[], items=[], stats={},
                materials={"Atem des Todes": 5, "Vergessene Seele": 2},
                confidence=1, source_images=["second.png"],
            ),
        ]

        result = analyze_many(
            [Path("first.png"), Path("second.png")], screen_type="materials"
        )

        self.assertEqual(
            result.materials,
            {"Arkanstaub": 10, "Atem des Todes": 5, "Vergessene Seele": 2},
        )
        self.assertEqual(result.source_images, ["first.png", "second.png"])
        self.assertEqual(
            [call.args[0].name for call in analyze_mock.call_args_list],
            ["first.png", "second.png"],
        )
        unload_mock.assert_called_once()
        self.assertTrue(
            all(not call.kwargs["unload_model"] for call in analyze_mock.call_args_list)
        )

    @patch("d3coach.unload")
    @patch("d3coach.extract_materials", return_value={"Arkanstaub": 29658})
    @patch("d3coach.locate", side_effect=AssertionError("guided mode must not locate"))
    def test_guided_material_view_uses_known_panel_region(self, _, materials_mock, __):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440), "black").save(image)
            result = analyze(image, screen_type="materials")

        self.assertEqual(result.screen_type, "materials")
        self.assertEqual(result.materials, {"Arkanstaub": 29658})
        self.assertEqual(result.panels[0].kind, "materials")
        materials_mock.assert_called_once()

    @patch("d3coach.unload")
    @patch("d3coach.extract_stats", return_value={"Kritische Trefferchance": "53,5%"})
    @patch("d3coach.extract_materials", return_value={"Arkanstaub": 29658})
    @patch("d3coach.locate")
    def test_routes_visible_lists_by_kind_and_owner(
        self, locate_mock, materials_mock, stats_mock, _
    ):
        locate_mock.return_value = Layout(
            screen_type="follower_stats",
            panels=[
                {
                    "kind": "player_stats", "role": "unknown", "owner": "player",
                    "title": "DETAILS", "box_1000": [100, 100, 300, 800],
                },
                {
                    "kind": "follower_stats", "role": "unknown", "owner": "follower",
                    "title": "DETAILS", "box_1000": [300, 100, 500, 800],
                },
                {
                    "kind": "materials", "role": "unknown", "owner": "player",
                    "title": "MATERIALIEN", "box_1000": [500, 100, 700, 800],
                },
            ],
            confidence=0.8,
        )
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (100, 100), "black").save(image)
            result = analyze(image)

        self.assertEqual(result.stats, {"Kritische Trefferchance": "53,5%"})
        self.assertEqual(result.follower_stats, {"Kritische Trefferchance": "53,5%"})
        self.assertEqual(result.materials, {"Arkanstaub": 29658})
        self.assertEqual(stats_mock.call_count, 2)
        self.assertEqual(materials_mock.call_count, 1)

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
            quality="legendary",
            is_set=True,
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
                patch.object(sys, "argv", ["d3coach.py", "analyze", str(image),
                                          "--state", str(Path(root) / "state.json")]),
                patch("builtins.print"),
            ):
                runpy.run_path("d3coach.py", run_name="__main__")


class RecognitionTest(unittest.TestCase):
    def test_discards_duplicate_item_box_and_keeps_equipped_role(self):
        layout = Layout.model_validate({
            "screen_type": "item_comparison", "confidence": 1,
            "panels": [
                {"kind": "item", "role": "candidate", "owner": "player",
                 "title": "Ring", "box_1000": [600, 0, 900, 700]},
                {"kind": "item", "role": "equipped", "owner": "player",
                 "title": "AUSGERÜSTET", "box_1000": [600, 0, 900, 700]},
            ],
        })

        self.assertEqual(len(layout.panels), 1)
        self.assertEqual(layout.panels[0].role, "equipped")
        self.assertEqual(layout.screen_type, "single_item")

    def test_ring_affix_armor_is_not_base_armor(self):
        raw = RawItem(
            role="equipped", owner="player", name="Die Windrose",
            quality_text="Set: Ring", slot="Finger", armor=380, dps=None,
            primary_affixes=["+380 Rüstung"], secondary_affixes=[],
            set_name="Endloser Gang", legendary_power=None,
        )

        item = normalize_item(raw, role="equipped", owner="player")

        self.assertIsNone(item.armor)
        self.assertIn("+380 Rüstung", item.primary_affixes)

    def test_legendary_gem_block_is_socket_content_not_item_power(self):
        gem = "Verderben der Mächtigen - Stufe 19\nErhöht Euren Schaden um 20%."
        raw = RawItem(
            role="equipped", owner="player", name="Band des leeren Raunens",
            quality_text="Uralter legendärer Ring", slot="Finger",
            armor=None, dps=None, primary_affixes=[], secondary_affixes=[],
            socket_effects=["Leerer Sockel"], set_name=None, legendary_power=gem,
        )

        item = normalize_item(raw, role="equipped", owner="player")

        self.assertIsNone(item.legendary_power)
        self.assertEqual(item.socket_effects, gem.splitlines())

    def test_weapon_dps_is_not_armor(self):
        raw = RawItem(
            role="candidate", owner="player", name="BURIZA-DO KYANON",
            quality_text="Archaische legendäre Armbrust", slot="Zweihand",
            armor=3160, dps=3160.5, primary_affixes=[],
            secondary_affixes=[], set_name=None, legendary_power=None,
        )

        item = normalize_item(raw, role="candidate", owner="player")

        self.assertEqual(item.quality, "primal")
        self.assertEqual(item.dps, 3160.5)
        self.assertIsNone(item.armor)

    def test_nonweapon_thorns_are_not_dps(self):
        raw = RawItem(
            role="candidate", owner="player", name="Magische Beinschienen",
            quality_text="Magischer Beinschutz", slot="Beine", armor=753,
            dps=2047, primary_affixes=[],
            secondary_affixes=["+2047 Dornenschaden"],
            set_name=None, legendary_power=None,
        )

        item = normalize_item(raw, role="candidate", owner="player")

        self.assertEqual(item.armor, 753)
        self.assertIsNone(item.dps)

    def test_gem_is_not_equipment(self):
        raw = RawItem(
            role="candidate", owner="player", name="MARQUISE-RUBIN",
            quality_text="Edelstein", slot="Sockel", armor=0, dps=0,
            primary_affixes=["Waffe: +160 Schaden"], secondary_affixes=[],
            set_name=None, legendary_power=None,
        )

        item = normalize_item(raw, role="candidate", owner="player")

        self.assertEqual(item.kind, "gem")
        self.assertIsNone(item.slot)
        self.assertIsNone(item.armor)
        self.assertIsNone(item.dps)

    def test_keeps_filled_and_empty_sockets_distinct(self):
        raw = RawItem(
            role="candidate", owner="player", name="Rüstung",
            quality_text="Uraltes Set: Brustschutz", slot="Körper",
            armor=688, dps=None, primary_affixes=[], secondary_affixes=[],
            socket_effects=["+280 Geschicklichkeit", "+280 Geschicklichkeit", "Leerer Sockel"],
            set_name="Unheilige Essenz", legendary_power=None,
        )

        item = normalize_item(raw, role="candidate", owner="player")

        self.assertEqual(item.socket_effects.count("+280 Geschicklichkeit"), 2)
        self.assertIn("Leerer Sockel", item.socket_effects)

    def test_recovers_gem_effect_misread_as_legendary_power(self):
        for effect in ("+280 Geschicklichkeit", "Kritischer Trefferschaden um 130,0% erhöht"):
            with self.subTest(effect=effect):
                raw = RawItem(
                    role="equipped", owner="player", name="Waffe",
                    quality_text="Uralter legendärer Bogen", slot="Zweihand",
                    armor=None, dps=3258.9, primary_affixes=[],
                    secondary_affixes=[], set_name=None, legendary_power=effect,
                )

                item = normalize_item(raw, role="equipped", owner="player")

                self.assertIsNone(item.legendary_power)
                self.assertEqual(item.socket_effects, [effect])

    def test_accepts_layout_confidence_as_percentage(self):
        layout = Layout.model_validate(
            {"screen_type": "other", "panels": [], "confidence": 100}
        )

        self.assertEqual(layout.confidence, 1.0)

    def test_extracts_visible_stats_and_material_counts_without_screenshots(self):
        replies = [
            {"values": {"Kritische Trefferchance": "53,5%"}},
            {"values": {"Arkanstaub": 29658}},
        ]
        requests = []

        def open_request(request, timeout):
            requests.append(json.loads(request.data))

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def read(self):
                    return json.dumps(
                        {"message": {"content": json.dumps(replies.pop(0))}}
                    ).encode()

            return Response()

        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "crop.png"
            Image.new("RGB", (10, 10), "black").save(image)
            stats = extract_stats(image, owner="follower", opener=open_request)
            materials = extract_materials(image, opener=open_request)

        self.assertEqual(stats, {"Kritische Trefferchance": "53,5%"})
        self.assertEqual(materials, {"Arkanstaub": 29658})
        self.assertIn("follower", requests[0]["messages"][0]["content"])
        self.assertIn("MATERIALIEN", requests[1]["messages"][0]["content"])

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
                        "quality": "legendary",
                        "is_set": True,
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
                    "quality": "legendary",
                    "is_set": True,
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
        self.assertEqual(item.quality, "ancient")
        self.assertTrue(item.is_set)
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

        self.assertEqual(item.quality, "legendary")
        self.assertTrue(item.is_set)

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

        self.assertEqual(item.quality, "ancient")
        self.assertTrue(item.is_set)
        self.assertIsNone(item.set_name)
        self.assertIsNone(item.legendary_power)

    def test_keeps_primal_quality_separate_from_set_type(self):
        raw = RawItem(
            role="candidate",
            owner="player",
            name="Testitem",
            quality_text="Archaisches Set: Brustschutz",
            slot="Körper",
            armor=1000,
            dps=None,
            primary_affixes=[],
            secondary_affixes=[],
            set_name="Aegis der Tapferkeit",
            legendary_power=None,
        )

        item = normalize_item(raw, role="candidate", owner="player")

        self.assertEqual(item.quality, "primal")
        self.assertTrue(item.is_set)

    def test_recovers_follower_item_name_from_misplaced_set_name(self):
        raw = RawItem(
            role="equipped",
            owner="follower",
            name="Legendärer Brustschutz",
            quality_text="Legendärer Brustschutz",
            slot="Körper",
            armor=748,
            dps=None,
            primary_affixes=[],
            secondary_affixes=[],
            set_name="GOLDHAUT",
            legendary_power="Gegner lassen manchmal Gold fallen.",
        )

        item = normalize_item(raw, role="equipped", owner="follower")

        self.assertEqual(item.name, "GOLDHAUT")
        self.assertEqual(item.quality, "legendary")
        self.assertFalse(item.is_set)
        self.assertIsNone(item.set_name)
        self.assertEqual(item.legendary_power, raw.legendary_power)

    def test_empty_set_name_does_not_make_a_legendary_item_a_set(self):
        raw = RawItem(
            role="equipped",
            owner="follower",
            name="Goldhaut",
            quality_text="Legendärer Brustschutz",
            slot="Körper",
            armor=748,
            dps=None,
            primary_affixes=[],
            secondary_affixes=[],
            set_name="",
            legendary_power=None,
        )

        item = normalize_item(raw, role="equipped", owner="follower")

        self.assertFalse(item.is_set)
        self.assertIsNone(item.set_name)

    def test_ignores_flavor_text_misread_as_set_name_and_power(self):
        flavor = "Die Dunkelheit wird von goldenem Glanz durchflutet."
        raw = RawItem(
            role="equipped",
            owner="follower",
            name="GOLDHAUT",
            quality_text="Legendärer Brustschutz",
            slot="Körper",
            armor=748,
            dps=None,
            primary_affixes=[],
            secondary_affixes=["Gegner lassen manchmal Gold fallen. (Strahlt aus)"],
            set_name=flavor,
            legendary_power=flavor,
        )

        item = normalize_item(raw, role="equipped", owner="follower")

        self.assertFalse(item.is_set)
        self.assertIsNone(item.set_name)
        self.assertIn("Strahlt aus", item.legendary_power)
        self.assertEqual(item.secondary_affixes, [])

    def test_recovers_follower_power_from_secondary_affix_not_socketed_gems(self):
        effect = "Gegner lassen manchmal Gold fallen. (Strahlt aus)"
        raw = RawItem(
            role="equipped",
            owner="follower",
            name="GOLDHAUT",
            quality_text="Legendärer Brustschutz",
            slot="Körper",
            armor=748,
            dps=None,
            primary_affixes=[],
            secondary_affixes=["+100% mehr Gold von Gegnern", effect],
            set_name=None,
            legendary_power="+220 Stärke\n+220 Stärke\n+220 Stärke",
        )

        item = normalize_item(raw, role="equipped", owner="follower")

        self.assertEqual(item.legendary_power, effect)
        self.assertEqual(item.secondary_affixes, ["+100% mehr Gold von Gegnern"])


if __name__ == "__main__":
    unittest.main()
