import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from d3coach import (
    Item, Recognition, CoachState, StoredItem, update_state, load_state,
    save_state, recommend_equip, disposal_advice,
    process_next, state_lock,
)


def item(name="Test"):
    return Item(role="candidate", owner="player", name=name, quality="legendary",
                is_set=False, slot="Kopf", armor=600, dps=None,
                primary_affixes=[], secondary_affixes=[], set_name=None,
                legendary_power=None)


def recognition(items=(), **changes):
    values = dict(screen_type="single_item", panels=[], items=list(items), stats={},
                  materials={}, confidence=1, source_images=["one.png"])
    values.update(changes)
    return Recognition(**values)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("d3coach.print", create=True))

    def test_roundtrip_and_partial_lists_preserve_unseen_values(self):
        state = CoachState()
        update_state(state, recognition(stats={"Leben": "1.000"}, materials={"Staub": 5}))
        update_state(state, recognition(stats={"Rüstung": "500"}, materials={"Teile": 2},
                                        source_images=["two.png"]))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            save_state(path, state)
            result = load_state(path)
        self.assertEqual(result.stats, {"Leben": "1.000", "Rüstung": "500"})
        self.assertEqual(result.materials, {"Staub": 5, "Teile": 2})
        self.assertEqual(result.character_class, "Crusader")

    def test_replay_is_idempotent_and_candidates_are_not_guessed_as_stash(self):
        state = CoachState()
        update_state(state, recognition([item()]))
        update_state(state, recognition([item()]))
        self.assertEqual(len(state.items), 1)
        self.assertEqual(next(iter(state.items.values())).location, "unknown")
        update_state(state, recognition([item()], source_images=["two.png"]), location="stash")
        self.assertEqual(len(state.items), 2)  # identical rolls can be two real items

    def test_assumed_equip_requires_replacement_action_and_observation_can_correct_it(self):
        state = CoachState(items={
            "old": StoredItem(item=item("Alt").model_copy(update={"role": "equipped"}),
                              location="equipped", evidence="observed"),
            "new": StoredItem(item=item("Neu"), location="inventory"),
        })
        advice = recommend_equip(state, "new")
        self.assertEqual(advice.replaced_item_id, "old")
        self.assertEqual(advice.replaced_action, "keep")
        self.assertEqual(state.items["new"].evidence, "assumed")
        update_state(state, recognition([item("Alt").model_copy(update={"role": "equipped"})]))
        self.assertEqual(state.items["old"].location, "equipped")
        self.assertEqual(state.items["new"].location, "unknown")
        self.assertEqual(len(state.items), 2)

    def test_primal_and_cube_uncertainty_are_never_salvaged(self):
        for quality, cube, expected in [("primal", "needed", "keep"),
                                         ("legendary", "unknown", "keep"),
                                         ("legendary", "needed", "cube"),
                                         ("legendary", "not_needed", "salvage")]:
            stored = StoredItem(item=item().model_copy(update={"quality": quality}),
                                cube_status=cube)
            self.assertEqual(disposal_advice(stored)[0], expected)

    def test_reroll_observation_updates_explicit_identity_and_keeps_primal_protection(self):
        state = CoachState(items={"a": StoredItem(
            item=item().model_copy(update={"quality": "primal"}), location="inventory")})
        update_state(state, recognition([item().model_copy(update={"armor": 700})]), item_id="a")
        self.assertEqual(state.items["a"].item.armor, 700)
        self.assertTrue(state.items["a"].souvenir)

    def test_explicit_item_id_updates_candidate_even_with_equipped_reference(self):
        state = CoachState(items={"a": StoredItem(item=item("Drop"), location="inventory")})
        result = recognition([item("Referenz").model_copy(update={"role": "equipped"}),
                              item("Drop").model_copy(update={"armor": 700})])
        update_state(state, result, item_id="a")
        self.assertEqual(state.items["a"].item.armor, 700)
        self.assertEqual(len(state.items), 2)

    def test_unknown_owner_and_ambiguous_slots_do_not_replace_equipment(self):
        state = CoachState(items={"a": StoredItem(item=item(), location="equipped")})
        update_state(state, recognition([item("Unknown").model_copy(
            update={"owner": "unknown", "role": "equipped"})]))
        self.assertEqual(state.items["a"].location, "equipped")
        with self.assertRaises(ValueError):
            recommend_equip(state, next(key for key in state.items if key != "a"))
        with self.assertRaises(ValueError):
            recommend_equip(CoachState(items={"r": StoredItem(
                item=item().model_copy(update={"slot": "Finger"}))}), "r")

    def test_invalid_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(path)
        with self.assertRaises(ValueError):
            CoachState(materials={"Staub": -1})
        with self.assertRaises(ValueError):
            CoachState(resources={"gold": -1})

    def test_state_lock_rejects_second_writer_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            with self.assertRaises(RuntimeError):
                with state_lock(path):
                    with self.assertRaises(TimeoutError):
                        with state_lock(path, timeout=0):
                            pass
                    raise RuntimeError("write failed")
            with state_lock(path, timeout=0):
                pass

    def test_state_cli_configures_progress_and_imports_saved_result(self):
        with tempfile.TemporaryDirectory() as root:
            path, result = Path(root) / "state.json", Path(root) / "result.json"
            result.write_text(recognition([item()]).model_dump_json(), encoding="utf-8")
            for arguments in [
                ["configure", "--season", "Testseason", "--goal", "Saisonreise"],
                ["progress", "season_journey", "Kapitel I", "done"],
                ["resource", "gold", "100"],
                ["ingest", str(result), "--location", "stash"],
            ]:
                with patch.object(sys, "argv", ["d3coach.py", "state", "--file", str(path), *arguments]), patch("builtins.print"):
                    runpy.run_path("d3coach.py", run_name="__main__")
            state = load_state(path)
            self.assertEqual(state.season_journey["Kapitel I"], "done")
            self.assertEqual(state.goals, ["Saisonreise"])
            self.assertEqual(state.resources["gold"], 100)
            self.assertEqual(next(iter(state.items.values())).location, "stash")
            key = next(iter(state.items))
            for arguments in [["item", key, "--souvenir"], ["equip", key]]:
                with patch.object(sys, "argv", ["d3coach.py", "state", "--file", str(path), *arguments]), patch("builtins.print"):
                    runpy.run_path("d3coach.py", run_name="__main__")
            self.assertEqual(load_state(path).items[key].evidence, "assumed")

    def test_follower_equip_does_not_replace_player_item(self):
        state = CoachState(items={
            "player": StoredItem(item=item(), location="equipped"),
            "follower": StoredItem(item=item().model_copy(update={"owner": "follower"})),
        })
        advice = recommend_equip(state, "follower")
        self.assertIsNone(advice.replaced_item_id)
        self.assertEqual(state.items["player"].location, "equipped")

    @patch("d3coach.analyze")
    def test_queue_updates_state_and_keeps_result_when_state_is_corrupt(self, analyze_mock):
        analyze_mock.return_value = recognition([item()])
        with tempfile.TemporaryDirectory() as root:
            queue, results = Path(root) / "queue", Path(root) / "results"
            incoming = queue / "incoming"
            incoming.mkdir(parents=True)
            (incoming / "a.png").write_bytes(b"image")
            state_path = Path(root) / "state.json"
            process_next(queue, results, state_path=state_path)
            self.assertEqual(len(load_state(state_path).items), 1)
            state_path.write_text("broken", encoding="utf-8")
            (incoming / "b.png").write_bytes(b"image")
            process_next(queue, results, state_path=state_path)
            self.assertTrue((results / "b.json").exists())
            self.assertTrue((queue / "failed" / "b.png").exists())
            self.assertEqual(state_path.read_text(), "broken")


if __name__ == "__main__":
    unittest.main()
