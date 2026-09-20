from __future__ import annotations

import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from openai_codex import (
    AsyncCodex,
    Codex,
    CodexConfig,
    LocalImageInput,
    Sandbox,
    TextInput,
    retry_on_overload,
)
from openai_codex.errors import CodexError as SdkCodexError
from openai_codex.types import ReasoningEffort


DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
DEFAULT_CODEX_EFFORT = "low"
CODEX_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_CODEX_PROMPT = (
    "Реши задание на приложенных скриншотах. Верни только готовый ответ обычным "
    "чистым текстом: без Markdown, заголовков, списочной разметки, жирного или "
    "курсивного выделения, обратных кавычек и декоративного оформления. Используй "
    "выделение или специальное оформление только если этого прямо требует условие "
    "задания. Не описывай работу с файлами и не отправляй ответ на сайт."
)


class CodexError(RuntimeError):
    """Codex SDK is unavailable, unauthenticated, or failed to solve a task."""


def codex_connection(model: str = DEFAULT_CODEX_MODEL) -> None:
    """Start the Python SDK and verify that the requested model is available."""
    try:
        with Codex() as codex:
            response = codex.models(include_hidden=False)
    except Exception as exc:
        raise CodexError(
            "Не удалось подключить Codex Python SDK. Выполните codex login"
        ) from exc

    available = {
        item.model
        for item in response.data
        if isinstance(getattr(item, "model", None), str)
    }
    if model not in available:
        raise CodexError(f"Модель {model!r} недоступна в текущей сессии Codex")


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


async def _run_codex_turn(
    screenshots: list[Path],
    *,
    prompt: str,
    model: str,
    effort: str,
    working_directory: Path,
) -> str:
    inputs = [TextInput(prompt)]
    inputs.extend(LocalImageInput(str(path.resolve())) for path in screenshots)
    config = CodexConfig(cwd=str(working_directory))
    async with AsyncCodex(config) as codex:
        thread = await codex.thread_start(
            cwd=str(working_directory),
            ephemeral=True,
            model=model,
            sandbox=Sandbox.read_only,
        )
        result = await thread.run(
            inputs,
            effort=ReasoningEffort(effort),
            model=model,
            sandbox=Sandbox.read_only,
        )
    return result.final_response


def _run_codex_turn_in_worker(
    screenshots: list[Path],
    *,
    prompt: str,
    model: str,
    effort: str,
    working_directory: Path,
    timeout_seconds: int,
) -> str:
    """Run the SDK loop outside Playwright's synchronous event-loop thread."""

    async def run_with_timeout() -> str:
        return await asyncio.wait_for(
            _run_codex_turn(
                screenshots,
                prompt=prompt,
                model=model,
                effort=effort,
                working_directory=working_directory,
            ),
            timeout=max(1, timeout_seconds),
        )

    return retry_on_overload(
        lambda: asyncio.run(run_with_timeout()),
        max_attempts=5,
        initial_delay_s=2,
        max_delay_s=15,
    )


def solve_with_codex(
    screenshots: list[Path],
    output_path: Path,
    *,
    prompt: str = DEFAULT_CODEX_PROMPT,
    model: str = DEFAULT_CODEX_MODEL,
    effort: str = DEFAULT_CODEX_EFFORT,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    if not screenshots:
        raise CodexError("Нет скриншотов для отправки в Codex")

    if effort not in CODEX_REASONING_EFFORTS:
        raise CodexError(f"Неизвестный уровень reasoning: {effort!r}")

    try:
        with tempfile.TemporaryDirectory(prefix="auto-hdp-codex-") as temporary:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="auto-hdp-codex") as pool:
                answer = pool.submit(
                    _run_codex_turn_in_worker,
                    screenshots,
                    prompt=prompt,
                    model=model,
                    effort=effort,
                    working_directory=Path(temporary),
                    timeout_seconds=timeout_seconds,
                ).result()
            answer = answer.strip()
    except TimeoutError as exc:
        raise CodexError(f"Codex не ответил за {timeout_seconds} секунд") from exc
    except SdkCodexError as exc:
        raise CodexError(f"Codex Python SDK завершился с ошибкой: {exc}") from exc
    except (OSError, RuntimeError) as exc:
        raise CodexError(f"Не удалось запустить Codex Python SDK: {exc}") from exc

    if not answer:
        raise CodexError("Codex вернул пустой ответ")
    output_path.write_text(f"{answer}\n", encoding="utf-8")
    return {
        "status": "solved",
        "provider": "openai-codex-python-sdk",
        "model": model,
        "reasoning_effort": effort,
        "prompt": prompt,
        "screenshots": [path.name for path in screenshots],
        "output": output_path.name,
    }
