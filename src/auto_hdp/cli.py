from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import Browser, BrowserType, Error as PlaywrightError, sync_playwright

from .capture import capture_task_page, safe_name, save_task_source, write_json
from .codex_solver import (
    CODEX_REASONING_EFFORTS,
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_PROMPT,
    CodexError,
    capture_screenshots,
    codex_connection,
    solve_with_codex,
)
from .platform import (
    HdpClient,
    HdpError,
    INELIGIBLE_TASK_PROGRESS_STATUSES,
    Subject,
    choose_latest_active_module,
    is_unsolved_level_task,
    select_module,
    select_subject,
    task_level,
    task_progress_status,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-hdp",
        description="Сохранить задания последнего активного модуля HDP",
    )
    parser.add_argument(
        "--subject",
        help="Точное название или ID предмета; без аргумента будет показан список",
    )
    parser.add_argument(
        "--module",
        help="Точное название или UUID модуля; без аргумента берётся последний активный",
    )
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=Path(".auth/storage-state.json"),
        help="Файл кешированной browser-сессии",
    )
    parser.add_argument("--base-url", default="https://horodigital.ru")
    parser.add_argument("--headed", action="store_true", help="Показать окно браузера")
    parser.add_argument("--timeout", type=int, default=30, help="Таймаут в секундах")
    parser.add_argument(
        "--all-tasks",
        action="store_true",
        help="Снять все задания, включая выполненные, проверяемые и уровень 4",
    )
    parser.add_argument(
        "--solve-with-codex",
        "--codex",
        action="store_true",
        help="Отправить скриншоты каждого выбранного задания через Codex Python SDK",
    )
    parser.add_argument(
        "--codex-prompt",
        default=DEFAULT_CODEX_PROMPT,
        help="Текст, отправляемый в Codex вместе со скриншотами",
    )
    parser.add_argument(
        "--codex-model",
        default=DEFAULT_CODEX_MODEL,
        help=f"Модель Codex SDK (по умолчанию: {DEFAULT_CODEX_MODEL})",
    )
    parser.add_argument(
        "--codex-effort",
        choices=CODEX_REASONING_EFFORTS,
        default=DEFAULT_CODEX_EFFORT,
        help=f"Уровень reasoning (по умолчанию: {DEFAULT_CODEX_EFFORT})",
    )
    parser.add_argument(
        "--codex-timeout",
        type=int,
        default=600,
        help="Таймаут одного ответа Codex в секундах",
    )
    return parser


def _interactive_subject(subjects: list[Subject]) -> Subject:
    if not subjects:
        raise HdpError("У ученика нет доступных предметов")
    print("Доступные предметы:")
    for index, subject in enumerate(subjects, start=1):
        print(
            f"  {index:>2}. {subject.name} "
            f"(ID {subject.discipline_id}, ступень {subject.stage_id})"
        )
    raw = input("Номер предмета: ").strip()
    if not raw.isdecimal() or not 1 <= int(raw) <= len(subjects):
        raise HdpError("Некорректный номер предмета")
    return subjects[int(raw) - 1]


def _public_module(module: dict[str, Any]) -> dict[str, Any]:
    keys = ("uuid", "title", "disciplineId", "stageId", "studyPeriod", "status")
    return {key: module.get(key) for key in keys}


def _launch_browser(browser_type: BrowserType, headed: bool) -> Browser:
    try:
        return browser_type.launch(headless=not headed)
    except PlaywrightError as original_error:
        for name in ("google-chrome", "google-chrome-stable", "chromium"):
            executable = shutil.which(name)
            if not executable:
                continue
            try:
                print(f"Playwright Chromium не найден; используется {executable}")
                return browser_type.launch(
                    headless=not headed,
                    executable_path=executable,
                )
            except PlaywrightError:
                continue
        raise HdpError(
            "Не найден запускаемый Chrome/Chromium. Выполните: "
            "playwright install chromium"
        ) from original_error


def _read_auth_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"Кеш сессии {path} повреждён и будет проигнорирован", file=sys.stderr)
        return None
    if not isinstance(value, dict) or not isinstance(value.get("cookies", []), list):
        print(f"Кеш сессии {path} имеет неверный формат", file=sys.stderr)
        return None
    return value


def _save_auth_state(context: Any, path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    context.storage_state(path=str(path))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _authenticate(
    client: HdpClient,
    login: str,
    password: str,
    had_cached_state: bool,
) -> None:
    if client.is_authenticated():
        print("Используется действующая кешированная сессия")
        return
    if had_cached_state:
        print("Кешированная сессия отклонена; выполняется повторный вход")
    else:
        print("Действующей сессии нет; выполняется вход")
    if not login or not password:
        raise HdpError(
            "Для обновления сессии добавьте HDP_LOGIN и HDP_PASSWORD в .env"
        )
    client.refresh_authentication()


def _run(args: argparse.Namespace) -> Path:
    load_dotenv(args.env_file)
    login = os.getenv("HDP_LOGIN", "").strip()
    password = os.getenv("HDP_PASSWORD", "")
    cached_state = _read_auth_state(args.auth_state)
    if args.solve_with_codex:
        codex_connection(args.codex_model)

    timeout_ms = max(1, args.timeout) * 1_000
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright.chromium, args.headed)
        context = browser.new_context(
            storage_state=cached_state,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            color_scheme="light",
        )
        context.set_default_timeout(timeout_ms)
        client = HdpClient(context, args.base_url)
        client.configure_reauthentication(
            login,
            password,
            lambda: _save_auth_state(context, args.auth_state),
        )
        try:
            _authenticate(client, login, password, cached_state is not None)
            subjects = client.subjects()
            subject = (
                select_subject(subjects, args.subject)
                if args.subject
                else _interactive_subject(subjects)
            )
            print(f"Предмет: {subject.name}")
            modules_by_status = client.modules(subject)
            module = (
                select_module(modules_by_status, args.module)
                if args.module
                else choose_latest_active_module(modules_by_status)
            )
            print(f"Модуль: {module.get('title', module.get('uuid'))}")
            module_data = client.module_with_tasks(str(module["uuid"]))
            visible_tasks = [
                task
                for task in module_data.get("tasks", [])
                if isinstance(task, dict) and task.get("hidden") is not True
            ]
            tasks = (
                visible_tasks
                if args.all_tasks
                else [task for task in visible_tasks if is_unsolved_level_task(task)]
            )
            if not args.all_tasks:
                print(
                    "Фильтр: уровни 1/2/3, не выполнено и не на проверке — "
                    f"{len(tasks)} из {len(visible_tasks)} заданий"
                )

            timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            run_dir = (
                args.output
                / safe_name(subject.name, f"subject-{subject.discipline_id}")
                / f"{timestamp}-{safe_name(str(module.get('title', 'module')))}"
            )
            run_dir.mkdir(parents=True, exist_ok=False)
            manifest: dict[str, Any] = {
                "created_at": datetime.now().astimezone().isoformat(),
                "base_url": args.base_url,
                "subject": {
                    "name": subject.name,
                    "discipline_id": subject.discipline_id,
                    "stage_id": subject.stage_id,
                },
                "module": _public_module(module_data),
                "task_count": len(tasks),
                "available_task_count": len(visible_tasks),
                "filter": {
                    "mode": "all" if args.all_tasks else "unsolved-levels-1-2-3",
                    "levels": None if args.all_tasks else [1, 2, 3],
                    "excluded_progress_statuses": None
                    if args.all_tasks
                    else sorted(INELIGIBLE_TASK_PROGRESS_STATUSES),
                },
                "solver": {
                    "enabled": args.solve_with_codex,
                    "provider": "openai-codex-python-sdk"
                    if args.solve_with_codex
                    else None,
                    "model": args.codex_model if args.solve_with_codex else None,
                    "reasoning_effort": args.codex_effort
                    if args.solve_with_codex
                    else None,
                    "prompt": args.codex_prompt if args.solve_with_codex else None,
                },
                "tasks": [],
            }
            write_json(run_dir / "manifest.json", manifest)
            page = context.new_page()
            for ordinal, task in enumerate(tasks, start=1):
                task_uuid = str(task.get("uuid", ""))
                task_title = str(task.get("title", f"task-{ordinal}"))
                task_dir = run_dir / f"{ordinal:03d}-{safe_name(task_title)}-{task_uuid[:8]}"
                task_dir.mkdir(parents=True)
                record: dict[str, Any] = {
                    "ordinal": ordinal,
                    "uuid": task_uuid,
                    "title": task_title,
                    "type": task.get("type"),
                    "status": task.get("status"),
                    "level_id": task_level(task),
                    "progress_status": task_progress_status(task),
                    "directory": task_dir.name,
                }
                print(f"[{ordinal}/{len(tasks)}] {task_title}")
                detail = task
                try:
                    detail = client.task_detail(str(module["uuid"]), task_uuid)
                except Exception as exc:
                    record["source_error"] = str(exc)
                    print(f"  Не удалось получить JSON условия: {exc}", file=sys.stderr)
                record["source"] = save_task_source(task_dir, detail)
                try:
                    record["capture"] = capture_task_page(
                        page,
                        client.task_url(str(module["uuid"]), task_uuid),
                        detail,
                        task_dir,
                        timeout_ms,
                        client.refresh_authentication,
                    )
                except Exception as exc:
                    record["capture"] = {"status": "error", "reason": str(exc)}
                    print(f"  Ошибка: {exc}", file=sys.stderr)
                if args.solve_with_codex:
                    screenshots = capture_screenshots(task_dir, record["capture"])
                    try:
                        record["solution"] = solve_with_codex(
                            screenshots,
                            task_dir / "solution.txt",
                            prompt=args.codex_prompt,
                            model=args.codex_model,
                            effort=args.codex_effort,
                            timeout_seconds=args.codex_timeout,
                        )
                        print(f"  Codex: {task_dir / 'solution.txt'}")
                    except CodexError as exc:
                        record["solution"] = {"status": "error", "reason": str(exc)}
                        print(f"  Ошибка Codex: {exc}", file=sys.stderr)
                manifest["tasks"].append(record)
                write_json(run_dir / "manifest.json", manifest)
                time.sleep(0.2)
            _save_auth_state(context, args.auth_state)
            return run_dir
        finally:
            context.close()
            browser.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        run_dir = _run(args)
    except (CodexError, HdpError, KeyboardInterrupt) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Готово: {run_dir.resolve()}")


if __name__ == "__main__":
    main()
