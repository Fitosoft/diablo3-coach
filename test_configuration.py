import unittest

from d3coach import CoachState
import game_de
import screen_layout


class ConfigurationTest(unittest.TestCase):
    def test_reference_layout_preserves_calibrated_crops(self):
        self.assertEqual(screen_layout.REFERENCE_SIZE, (2560, 1440))
        self.assertEqual(screen_layout.HALF_SCREEN_FACTOR, 0.5)
        self.assertEqual(screen_layout.HALF_BOXES,
                         [[0, 0, 516, 1000], [484, 0, 1000, 1000]])
        self.assertEqual(screen_layout.HEADER_FRAMES[2], (1450, 15, 1780, 55))

    def test_german_game_labels_remain_separate_from_english_defaults(self):
        self.assertIn("TEMPLER", game_de.FOLLOWER_TITLES)
        self.assertIn("MATERIALIEN", game_de.VIEW_PROMPT)
        state = CoachState()
        self.assertEqual(state.character_class, "Crusader")
        self.assertEqual(state.build, "Aegis of Valor")
        self.assertEqual(state.goals, ["Complete the Season Journey", "Upgrade the Altar"])
        old = CoachState(character_class="Kreuzritter", goals=["Saisonreise"])
        self.assertEqual(old.character_class, "Kreuzritter")
        self.assertEqual(old.goals, ["Saisonreise"])


if __name__ == "__main__":
    unittest.main()
