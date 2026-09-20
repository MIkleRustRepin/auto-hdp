from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import time
from typing import Any, Callable

from playwright.sync_api import APIResponse, BrowserContext, Error as PlaywrightError


class HdpError(RuntimeError):
    """A platform request failed or returned an unexpected payload."""


@dataclass(frozen=True)
class Subject:
    discipline_id: int
    stage_id: int
    name: str


INELIGIBLE_TASK_PROGRESS_STATUSES = frozenset(
    {
        "accepted",
        "approved",
        "completed",
        "done",
        "passed",
        "reviewing",
        "submitted",
    }
)


def task_level(task: dict[str, Any]) -> int | None:
    """Return an integer HDP level for values such as 1, 1.0, or "1.0"."""
    value = task.get("levelId")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric.is_integer():
        return None
    return int(numeric)


def task_progress_status(task: dict[str, Any]) -> str | None:
    progress = task.get("progress")
    if not isinstance(progress, dict):
        return None
    status = progress.get("status")
    if not isinstance(status, dict):
        return None
    value = status.get("type")
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def is_unsolved_level_task(task: dict[str, Any]) -> bool:
    """Select unfinished/non-reviewing tasks from HDP levels 1, 2, and 3."""
    return (
        task.get("hidden") is not True
        and task_level(task) in {1, 2, 3}
        and task_progress_status(task) not in INELIGIBLE_TASK_PROGRESS_STATUSES
    )


def _date_key(module: dict[str, Any]) -> tuple[date, str]:
    period = module.get("studyPeriod") or {}
    raw = period.get("startDate") or "0001-01-01"
    try:
        parsed = date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        parsed = date.min
    return parsed, str(module.get("uuid", ""))


def choose_latest_active_module(
    modules_by_status: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Choose the newest started module, or the newest finished one as fallback."""
    started = modules_by_status.get("started", [])
    if started:
        return max(started, key=_date_key)
    finished = modules_by_status.get("finished", [])
    if finished:
        return max(finished, key=_date_key)
    raise HdpError("У предмета нет активного или завершённого модуля")


def select_module(
    modules_by_status: dict[str, list[dict[str, Any]]],
    query: str,
) -> dict[str, Any]:
    """Select a module by exact case-insensitive title or UUID."""
    normalized = query.strip().casefold()
    unique: dict[str, dict[str, Any]] = {}
    for status, modules in modules_by_status.items():
        for module in modules:
            module_uuid = str(module.get("uuid", ""))
            if not module_uuid:
                continue
            item = dict(module)
            item.setdefault("listStatus", status)
            unique[module_uuid] = item
    matches = [
        module
        for module in unique.values()
        if str(module.get("uuid", "")).casefold() == normalized
        or str(module.get("title", "")).strip().casefold() == normalized
    ]
    if not matches:
        raise HdpError(f"Модуль {query!r} не найден у выбранного предмета")
    if len(matches) > 1:
        options = ", ".join(
            f"{module.get('title')} ({module.get('uuid')})" for module in matches
        )
        raise HdpError(f"Название модуля неоднозначно: {options}. Используйте UUID")
    return matches[0]


def select_subject(subjects: list[Subject], query: str) -> Subject:
    normalized = query.strip().casefold()
    if normalized.isdecimal():
        matches = [item for item in subjects if item.discipline_id == int(normalized)]
    else:
        matches = [item for item in subjects if item.name.strip().casefold() == normalized]
    if not matches:
        raise HdpError(f"Предмет {query!r} не найден среди предметов ученика")
    if len(matches) > 1:
        options = ", ".join(
            f"{item.name} (ID {item.discipline_id}, ступень {item.stage_id})"
            for item in matches
        )
        raise HdpError(f"Название неоднозначно: {options}. Используйте ID предмета")
    return matches[0]


class HdpClient:
    def __init__(self, context: BrowserContext, base_url: str) -> None:
        self.context = context
        self.base_url = base_url.rstrip("/")
        self._credentials: tuple[str, str] | None = None
        self._on_auth_refreshed: Callable[[], None] | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _payload(response: APIResponse, operation: str) -> Any:
        if not response.ok:
            raise HdpError(f"{operation}: HTTP {response.status}")
        try:
            payload = response.json()
        except Exception as exc:
            raise HdpError(f"{operation}: сервер вернул не JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise HdpError(f"{operation}: платформа сообщила об ошибке")
        return payload.get("data")

    def login(self, login: str, password: str) -> None:
        response = self.context.request.post(
            self._url("/api/auth/v1/login"),
            data={"login": login, "password": password},
            headers={
                "Origin": self.base_url,
                "Referer": self._url("/login"),
            },
        )
        self._payload(response, "Авторизация")

    def configure_reauthentication(
        self,
        login: str,
        password: str,
        on_auth_refreshed: Callable[[], None] | None = None,
    ) -> None:
        if login and password:
            self._credentials = (login, password)
        self._on_auth_refreshed = on_auth_refreshed

    def is_authenticated(self) -> bool:
        response = self.context.request.get(self._url("/api/auth/v1/currentUser"))
        if response.status in (401, 403):
            return False
        if not response.ok:
            raise HdpError(f"Проверка сессии: HTTP {response.status}")
        try:
            payload = response.json()
        except Exception as exc:
            raise HdpError("Проверка сессии: сервер вернул не JSON") from exc
        return isinstance(payload, dict) and payload.get("success") is True

    def refresh_authentication(self) -> None:
        if self._credentials is None:
            raise HdpError(
                "Сессия истекла, а HDP_LOGIN/HDP_PASSWORD не заданы для обновления"
            )
        self.login(*self._credentials)
        if not self.is_authenticated():
            raise HdpError("Обновлённая сессия не прошла проверку currentUser")
        if self._on_auth_refreshed:
            self._on_auth_refreshed()

    def _get(self, path: str, operation: str) -> APIResponse:
        response: APIResponse | None = None
        for attempt in range(3):
            try:
                response = self.context.request.get(self._url(path))
                break
            except PlaywrightError:
                if attempt == 2:
                    raise HdpError(
                        f"{operation}: сетевая ошибка после трёх попыток"
                    ) from None
                time.sleep(0.5 * (attempt + 1))
        if response is None:
            raise HdpError(f"{operation}: запрос не вернул ответ")
        if response.status in (401, 403):
            self.refresh_authentication()
            try:
                response = self.context.request.get(self._url(path))
            except PlaywrightError:
                raise HdpError(
                    f"{operation}: сетевая ошибка после обновления авторизации"
                ) from None
        if response.status in (401, 403):
            raise HdpError(f"{operation}: доступ запрещён после обновления сессии")
        return response

    def subjects(self) -> list[Subject]:
        enrolled_response = self._get(
            "/api/education/v1/students/current/disciplines",
            "Получение предметов ученика",
        )
        enrolled = self._payload(enrolled_response, "Получение предметов ученика")
        catalog_response = self._get(
            "/api/schools/v1/current/disciplines",
            "Получение названий предметов",
        )
        catalog = self._payload(catalog_response, "Получение названий предметов")
        names = {
            item.get("disciplineId"): item.get("name", f"Предмет {item.get('disciplineId')}")
            for item in catalog or []
        }
        result = []
        for item in enrolled or []:
            discipline_id = item.get("disciplineId")
            stage_id = item.get("stageId")
            if isinstance(discipline_id, int) and isinstance(stage_id, int):
                result.append(
                    Subject(
                        discipline_id=discipline_id,
                        stage_id=stage_id,
                        name=str(names.get(discipline_id, f"Предмет {discipline_id}")),
                    )
                )
        return sorted(result, key=lambda item: (item.name.casefold(), item.stage_id))

    def modules(self, subject: Subject) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        base = (
            "/api/education/v1/students/current/disciplines/"
            f"{subject.discipline_id}/stages/{subject.stage_id}/topics"
        )
        for status in ("started", "planned", "finished"):
            response = self._get(f"{base}/{status}", f"Получение модулей ({status})")
            data = self._payload(response, f"Получение модулей ({status})")
            result[status] = data if isinstance(data, list) else []
        return result

    def module_with_tasks(self, module_uuid: str) -> dict[str, Any]:
        response = self._get(
            f"/api/education/v1/students/current/topics/{module_uuid}/tasks",
            "Получение заданий модуля",
        )
        data = self._payload(response, "Получение заданий модуля")
        if not isinstance(data, dict):
            raise HdpError("Получение заданий модуля: неожиданная схема ответа")
        return data

    def task_detail(self, module_uuid: str, task_uuid: str) -> dict[str, Any]:
        response = self._get(
            "/api/education/v1/students/current/topics/"
            f"{module_uuid}/tasks/{task_uuid}",
            f"Получение задания {task_uuid}",
        )
        data = self._payload(response, f"Получение задания {task_uuid}")
        if not isinstance(data, dict):
            raise HdpError("Получение задания: неожиданная схема ответа")
        return data

    def task_url(self, module_uuid: str, task_uuid: str) -> str:
        return self._url(f"/student/topic/{module_uuid}/task/{task_uuid}")
