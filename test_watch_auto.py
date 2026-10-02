import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from d3coach import ViewContext, ViewTitles, analyze, process_next, Recognition, classify_view, Layout, Item, CoachState, update_state


class AutomaticWatchTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("d3coach.print", create=True))

    def test_title_routing_and_no_invented_owner_from_party_portrait(self):
        titles = ViewTitles(left_title="", middle_left_title="",
                            middle_right_title="DETAILS", right_title="INVENTAR")
        self.assertEqual(titles.context().screen_type, "player_stats")
        self.assertFalse(titles.context().follower_open)
        titles.left_title, titles.middle_left_title, titles.middle_right_title = "TEMPLER", "DETAILS", ""
        self.assertEqual(titles.context().screen_type, "follower_stats")

    @patch("d3coach._ask", return_value='{"title":"MATERIALIEN"}')
    def test_blank_headers_skip_gpu_and_visible_frame_reads_only_its_title(self, ask):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            source = Image.new("RGB", (2560, 1440))
            source.save(image)
            self.assertEqual(classify_view(image).screen_type, "items_or_other")
            ask.assert_not_called()
            source.paste((100, 0, 0), (1450, 15, 1780, 55))
            source.save(image)
            self.assertEqual(classify_view(image).screen_type, "materials")
            ask.assert_called_once()
    @patch("d3coach.unload")
    @patch("d3coach.extract_materials", return_value={"Staub": 7})
    @patch("d3coach.classify_view", return_value=ViewContext(screen_type="materials"))
    @patch("d3coach.locate_with_fallback", side_effect=AssertionError("fixed list crop should be used"))
    def test_auto_materials_use_fixed_crop(self, _, classify, extract, unload):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440)).save(image)
            result = analyze(image)
        self.assertEqual(result.materials, {"Staub": 7})
        unload.assert_called_once()

    @patch("d3coach.unload")
    @patch("d3coach.extract_stats", return_value={"Leben": "1.000"})
    @patch("d3coach.classify_view", return_value=ViewContext(screen_type="follower_stats", follower_open=True))
    def test_auto_follower_stats_remain_separate(self, classify, extract, unload):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440)).save(image)
            result = analyze(image)
        self.assertEqual(result.follower_stats, {"Leben": "1.000"})
        self.assertEqual(result.stats, {})

    @patch("d3coach.unload")
    @patch("d3coach.extract_item")
    @patch("d3coach.locate_with_fallback")
    @patch("d3coach.classify_view", return_value=ViewContext(stash_open=True, inventory_open=True))
    def test_comparison_keeps_reference_and_assigns_candidate_to_stash(self, classify, locate, extract, unload):
        locate.return_value = Layout(screen_type="item_comparison", confidence=1, panels=[
            {"kind": "item", "role": "candidate", "owner": "player", "title": "Drop", "box_1000": [200, 0, 450, 900]},
            {"kind": "item", "role": "equipped", "owner": "player", "title": "Alt", "box_1000": [500, 0, 750, 900]},
        ])
        base = Item(role="candidate", owner="player", name="Drop", quality="legendary", is_set=False,
                    slot="Kopf", armor=1, dps=None, primary_affixes=[], secondary_affixes=[], set_name=None, legendary_power=None)
        extract.side_effect = [base, base.model_copy(update={"role": "equipped", "name": "Alt"})]
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "screen.png"
            Image.new("RGB", (2560, 1440)).save(image)
            result = analyze(image)
        state = CoachState()
        update_state(state, result)
        self.assertEqual(len(state.items), 2)
        self.assertEqual({value.item.name: value.location for value in state.items.values()},
                         {"Drop": "stash", "Alt": "equipped"})

    @patch("d3coach.analyze")
    def test_empty_recognition_goes_to_review_without_updating_state(self, analyze_mock):
        analyze_mock.return_value = Recognition(screen_type="other", panels=[], items=[],
                                               stats={}, materials={}, confidence=1)
        with tempfile.TemporaryDirectory() as root:
            queue, results = Path(root) / "queue", Path(root) / "results"
            (queue / "incoming").mkdir(parents=True)
            (queue / "incoming" / "a.png").write_bytes(b"image")
            process_next(queue, results)
            self.assertTrue((queue / "review" / "a.png").exists())
            self.assertFalse((Path(root) / "state.json").exists())

    def test_file_still_being_written_is_left_in_incoming(self):
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "queue"
            (queue / "incoming").mkdir(parents=True)
            image = queue / "incoming" / "a.png"
            image.write_bytes(b"part")
            with patch("d3coach.sleep", side_effect=lambda _: image.write_bytes(b"more bytes")):
                self.assertFalse(process_next(queue, Path(root) / "results"))
            self.assertTrue(image.exists())

    @patch("d3coach.analyze")
    def test_new_earlier_filename_does_not_bypass_stability_check(self, analyze_mock):
        analyze_mock.return_value = Recognition(screen_type="materials", panels=[], items=[],
                                               stats={}, materials={"Staub": 1}, confidence=1)
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "queue"
            incoming = queue / "incoming"
            incoming.mkdir(parents=True)
            (incoming / "a.png").write_bytes(b"image")
            with patch("d3coach.sleep", side_effect=lambda _: (incoming / "0.png").write_bytes(b"part")):
                process_next(queue, Path(root) / "results")
            self.assertEqual(analyze_mock.call_args.args[0].name, "a.png")
            self.assertTrue((incoming / "0.png").exists())

    @patch("d3coach.analyze")
    def test_restart_recovers_processing_before_new_screenshots(self, analyze_mock):
        analyze_mock.return_value = Recognition(screen_type="materials", panels=[], items=[],
                                               stats={}, materials={"Staub": 1}, confidence=1)
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "queue"
            (queue / "processing").mkdir(parents=True)
            (queue / "processing" / "old.png").write_bytes(b"image")
            self.assertTrue(process_next(queue, Path(root) / "results"))
            self.assertTrue((queue / "done" / "old.png").exists())


if __name__ == "__main__":
    unittest.main()
