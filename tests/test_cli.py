import unittest
import tempfile
from pathlib import Path

from auto_hdp.cli import _capture_from_disk, _safe_error


class CliHelpersTest(unittest.TestCase):
    def test_capture_metadata_is_reconstructed_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "page.png").write_bytes(b"png")
            (directory / "page.html").write_text("<html></html>", encoding="utf-8")
            (directory / "question-001.png").write_bytes(b"png")

            capture = _capture_from_disk(directory)

            self.assertEqual(capture["status"], "captured")
            self.assertEqual(capture["artifacts"]["screenshot"], "page.png")
            self.assertEqual(capture["questions"][0]["screenshot"], "question-001.png")

    def test_safe_error_removes_request_call_log(self) -> None:
        error = RuntimeError(
            "APIRequestContext.get: socket hang up\n"
            "Call log:\n"
            "  cookie: hdp-portal-session=secret"
        )
        self.assertEqual(_safe_error(error), "APIRequestContext.get: socket hang up")


if __name__ == "__main__":
    unittest.main()
