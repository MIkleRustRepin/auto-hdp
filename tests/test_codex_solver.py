import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_hdp.codex_solver import capture_screenshots, solve_with_codex


class CodexSolverTest(unittest.TestCase):
    def test_question_screenshots_are_preferred_over_full_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("page.png", "question-001.png", "question-002.png"):
                (directory / name).write_bytes(b"png")
            screenshots = capture_screenshots(
                directory,
                {
                    "artifacts": {"screenshot": "page.png"},
                    "questions": [
                        {"screenshot": "question-001.png"},
                        {"screenshot": "question-002.png"},
                    ],
                },
            )
            self.assertEqual(
                [path.name for path in screenshots],
                ["question-001.png", "question-002.png"],
            )

    def test_codex_answer_is_written_to_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            screenshot = directory / "page.png"
            screenshot.write_bytes(b"png")
            output = directory / "solution.txt"

            def fake_run(command, **_kwargs):
                message_path = Path(command[command.index("--output-last-message") + 1])
                message_path.write_text("Готовый ответ", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("auto_hdp.codex_solver.subprocess.run", side_effect=fake_run) as run:
                result = solve_with_codex(
                    "/usr/bin/codex",
                    [screenshot],
                    output,
                    prompt="Реши",
                    timeout_seconds=20,
                )

            command = run.call_args.args[0]
            self.assertIn("--ephemeral", command)
            self.assertIn("read-only", command)
            self.assertEqual(command[-1], "Реши")
            self.assertEqual(output.read_text(encoding="utf-8"), "Готовый ответ\n")
            self.assertEqual(result["status"], "solved")


if __name__ == "__main__":
    unittest.main()
