"""Documentation contracts; these do not certify agent or generator behavior."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills" / "note-taking"


class LearningReviewContractTests(unittest.TestCase):
    def test_both_skill_entry_points_reach_the_same_review_reference(self):
        reference = SKILLS / "my-journal" / "references" / "learning-review.md"
        for name in ("journal", "my-journal"):
            skill = SKILLS / name / "SKILL.md"
            text = skill.read_text(encoding="utf-8")
            with self.subTest(skill=name):
                links = re.findall(r"\[[^\]]+\]\(([^)]+learning-review\.md)\)", text)
                self.assertEqual(len(links), 1, "Link the shared learning review procedure")
                self.assertEqual((skill.parent / links[0]).resolve(), reference.resolve())
                self.assertTrue(reference.is_file())


if __name__ == "__main__":
    unittest.main()
