from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillSchedulingContractTests(unittest.TestCase):
    def test_scheduled_workflow_requires_exact_resume_and_never_fresh_collection(self):
        text = (ROOT / "skills" / "note-taking" / "my-journal" / "SKILL.md").read_text()
        scheduled = text.split("## Scheduling", 1)[1].split("## Common Pitfalls", 1)[0]
        self.assertIn("journal_generation_resume", scheduled)
        self.assertIn("binding_id", scheduled)
        self.assertIn("must never call `journal_generation_collect`", scheduled)
        self.assertIn("required post-run", scheduled)


if __name__ == "__main__":
    unittest.main()
