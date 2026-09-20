import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import LocalImageInput, Sandbox, ServerBusyError, TextInput
from openai_codex.types import ReasoningEffort

from auto_hdp.codex_solver import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_PROMPT,
    _run_codex_turn,
    capture_screenshots,
    solve_with_codex,
)


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

            with patch(
                "auto_hdp.codex_solver._run_codex_turn",
                new_callable=AsyncMock,
                return_value="Готовый ответ",
            ) as run:

                async def call_while_event_loop_is_running():
                    return solve_with_codex([screenshot], output, timeout_seconds=20)

                result = asyncio.run(call_while_event_loop_is_running())

            self.assertEqual(run.await_args.kwargs["model"], "gpt-5.6-terra")
            self.assertEqual(run.await_args.kwargs["effort"], "low")
            self.assertIn("без Markdown", run.await_args.kwargs["prompt"])
            self.assertEqual(output.read_text(encoding="utf-8"), "Готовый ответ\n")
            self.assertEqual(result["provider"], "openai-codex-python-sdk")
            self.assertEqual(result["reasoning_effort"], "low")

    def test_sdk_turn_sends_prompt_and_local_images_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            screenshot = directory / "page.png"
            screenshot.write_bytes(b"png")
            observed = {}

            class FakeThread:
                async def run(self, inputs, **kwargs):
                    observed["inputs"] = inputs
                    observed["run"] = kwargs
                    return SimpleNamespace(final_response="Ответ")

            class FakeCodex:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return None

                async def thread_start(self, **kwargs):
                    observed["start"] = kwargs
                    return FakeThread()

            with patch("auto_hdp.codex_solver.AsyncCodex", return_value=FakeCodex()):
                answer = asyncio.run(
                    _run_codex_turn(
                        [screenshot],
                        prompt=DEFAULT_CODEX_PROMPT,
                        model=DEFAULT_CODEX_MODEL,
                        effort=DEFAULT_CODEX_EFFORT,
                        working_directory=directory,
                    )
                )

            self.assertEqual(answer, "Ответ")
            self.assertIsInstance(observed["inputs"][0], TextInput)
            self.assertIsInstance(observed["inputs"][1], LocalImageInput)
            self.assertEqual(observed["start"]["model"], "gpt-5.6-terra")
            self.assertEqual(observed["start"]["sandbox"], Sandbox.read_only)
            self.assertEqual(observed["run"]["effort"], ReasoningEffort.low)

    def test_transient_capacity_error_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            screenshot = directory / "page.png"
            screenshot.write_bytes(b"png")
            output = directory / "solution.txt"

            with (
                patch(
                    "auto_hdp.codex_solver._run_codex_turn",
                    new_callable=AsyncMock,
                    side_effect=[ServerBusyError(-1, "at capacity"), "Ответ после повтора"],
                ) as run,
                patch("openai_codex.retry.time.sleep"),
            ):
                result = solve_with_codex([screenshot], output, timeout_seconds=20)

            self.assertEqual(run.await_count, 2)
            self.assertEqual(result["status"], "solved")
            self.assertEqual(output.read_text(encoding="utf-8"), "Ответ после повтора\n")


if __name__ == "__main__":
    unittest.main()
