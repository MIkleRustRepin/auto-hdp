import unittest
from unittest.mock import patch

from playwright.sync_api import Error as PlaywrightError

from auto_hdp.platform import (
    HdpClient,
    HdpError,
    Subject,
    choose_latest_active_module,
    is_unsolved_level_task,
    select_module,
    select_subject,
    task_level,
    task_progress_status,
)


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.ok = 200 <= status < 300
        self._payload = payload if payload is not None else {"success": True, "data": {}}

    def json(self):
        return self._payload


class FakeRequest:
    def __init__(self, gets, posts=None):
        self.gets = list(gets)
        self.posts = list(posts or [])

    def get(self, _url):
        result = self.gets.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def post(self, _url, **_kwargs):
        return self.posts.pop(0)


class FakeContext:
    def __init__(self, request):
        self.request = request


class PlatformSelectionTest(unittest.TestCase):
    def test_unsolved_task_filter_uses_level_and_progress(self) -> None:
        task = {
            "levelId": 3.0,
            "progress": {"status": {"type": "appointed"}},
        }
        self.assertTrue(is_unsolved_level_task(task))
        self.assertEqual(task_level(task), 3)
        self.assertEqual(task_progress_status(task), "appointed")

        for status in ("approved", "reviewing", "submitted"):
            task["progress"]["status"]["type"] = status
            self.assertFalse(is_unsolved_level_task(task))

        task["progress"]["status"]["type"] = "appointed"
        task["levelId"] = "4.0"
        self.assertFalse(is_unsolved_level_task(task))

    def test_unsolved_task_filter_accepts_missing_progress(self) -> None:
        self.assertTrue(is_unsolved_level_task({"levelId": "1.0"}))
        self.assertFalse(is_unsolved_level_task({"levelId": 2, "hidden": True}))
        self.assertFalse(is_unsolved_level_task({"levelId": "unknown"}))

    def test_latest_started_wins_over_newer_planned(self) -> None:
        selected = choose_latest_active_module(
            {
                "started": [
                    {"uuid": "old", "studyPeriod": {"startDate": "2026-08-01"}},
                    {"uuid": "new", "studyPeriod": {"startDate": "2026-09-01"}},
                ],
                "planned": [
                    {"uuid": "future", "studyPeriod": {"startDate": "2026-10-01"}}
                ],
                "finished": [],
            }
        )
        self.assertEqual(selected["uuid"], "new")

    def test_finished_is_fallback(self) -> None:
        selected = choose_latest_active_module(
            {
                "started": [],
                "finished": [
                    {"uuid": "a", "studyPeriod": {"startDate": "2026-01-01"}},
                    {"uuid": "b", "studyPeriod": {"startDate": "2026-02-01"}},
                ],
            }
        )
        self.assertEqual(selected["uuid"], "b")

    def test_subject_can_be_selected_by_name_or_id(self) -> None:
        subjects = [Subject(5, 10, "Биология"), Subject(8, 10, "Математика")]
        self.assertEqual(select_subject(subjects, "биология").discipline_id, 5)
        self.assertEqual(select_subject(subjects, "8").name, "Математика")
        with self.assertRaises(HdpError):
            select_subject(subjects, "Физика")

    def test_module_can_be_selected_by_title_or_uuid(self) -> None:
        modules = {
            "started": [
                {"uuid": "module-a", "title": "Г1. Введение в стереометрию"}
            ],
            "planned": [{"uuid": "module-b", "title": "Г2. Параллельность"}],
            "finished": [],
        }
        self.assertEqual(
            select_module(modules, "г1. введение в стереометрию")["uuid"],
            "module-a",
        )
        self.assertEqual(select_module(modules, "module-b")["title"], "Г2. Параллельность")
        with self.assertRaises(HdpError):
            select_module(modules, "Несуществующий модуль")


class AuthenticationTest(unittest.TestCase):
    def test_network_get_is_retried_without_leaking_call_log(self) -> None:
        request = FakeRequest(
            gets=[
                PlaywrightError("socket hang up\nCall log:\n  cookie: secret"),
                FakeResponse(200, {"success": True, "data": ["ok"]}),
            ]
        )
        client = HdpClient(FakeContext(request), "https://example.test")

        with patch("auto_hdp.platform.time.sleep") as sleep:
            response = client._get("/tasks", "Получение заданий")

        self.assertEqual(response.json()["data"], ["ok"])
        sleep.assert_called_once()

    def test_denied_get_refreshes_session_and_retries_once(self) -> None:
        request = FakeRequest(
            gets=[
                FakeResponse(401),
                FakeResponse(200),  # currentUser after login
                FakeResponse(200, {"success": True, "data": ["ok"]}),
            ],
            posts=[FakeResponse(200)],
        )
        client = HdpClient(FakeContext(request), "https://example.test")
        refreshed = []
        client.configure_reauthentication("login", "password", lambda: refreshed.append(True))

        response = client._get("/protected", "Protected request")

        self.assertEqual(response.json()["data"], ["ok"])
        self.assertEqual(refreshed, [True])
        self.assertEqual(request.gets, [])
        self.assertEqual(request.posts, [])

    def test_denied_session_without_credentials_is_clear_error(self) -> None:
        client = HdpClient(
            FakeContext(FakeRequest(gets=[FakeResponse(403)])),
            "https://example.test",
        )
        with self.assertRaisesRegex(HdpError, "HDP_LOGIN/HDP_PASSWORD"):
            client._get("/protected", "Protected request")


if __name__ == "__main__":
    unittest.main()
