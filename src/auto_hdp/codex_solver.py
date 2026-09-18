from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class CodexError(RuntimeError):
    """Codex CLI is unavailable, unauthenticated, or failed to solve a task."""


def codex_connection() -> str:
    executable = shutil.which("codex")
    if not executable:
        raise CodexError("Codex CLI не найден. Установите его и выполните: codex login")
    result = subprocess.run(
        [executable, "login", "status"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise CodexError("Codex CLI не авторизован. Выполните: codex login")
    return executable


def capture_screenshots(directory: Path, capture: dict[str, Any]) -> list[Path]:
    """Prefer one screenshot per test question, otherwise use the full task page."""
    names: list[str] = []
    questions = capture.get("questions")
    if isinstance(questions, list):
        for question in questions:
            if isinstance(question, dict) and isinstance(question.get("screenshot"), str):
                names.append(question["screenshot"])
    if not names:
        artifacts = capture.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("screenshot"), str):
            names.append(artifacts["screenshot"])

    result: list[Path] = []
    seen: set[Path] = set()
    for name in names:
        path = (directory / name).resolve()
        if path.is_file() and path not in seen:
            seen.add(path)
            result.append(path)
    return result


def solve_with_codex(
    executable: str,
    screenshots: list[Path],
    output_path: Path,
    *,
    prompt: str = "Реши",
    model: str | None = None,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    if not screenshots:
        raise CodexError("Нет скриншотов для отправки в Codex")

    with tempfile.TemporaryDirectory(prefix="auto-hdp-codex-") as temporary:
        temporary_dir = Path(temporary)
        last_message = temporary_dir / "last-message.txt"
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            str(temporary_dir),
            "--output-last-message",
            str(last_message),
        ]
        if model:
            command.extend(["--model", model])
        for screenshot in screenshots:
            command.extend(["--image", str(screenshot.resolve())])
        command.extend(["--", prompt])

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(1, timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexError(f"Codex не ответил за {timeout_seconds} секунд") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "неизвестная ошибка").strip()
            raise CodexError(f"Codex завершился с ошибкой: {message[-2000:]}")
        if not last_message.is_file():
            raise CodexError("Codex не создал итоговый ответ")
        answer = last_message.read_text(encoding="utf-8").strip()
        if not answer:
            raise CodexError("Codex вернул пустой ответ")

    output_path.write_text(f"{answer}\n", encoding="utf-8")
    return {
        "status": "solved",
        "provider": "codex-cli",
        "model": model or "configured-default",
        "prompt": prompt,
        "screenshots": [path.name for path in screenshots],
        "output": output_path.name,
    }
