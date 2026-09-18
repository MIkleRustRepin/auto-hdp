from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError


QUESTION_COUNTER = re.compile(r"Вопрос\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def safe_name(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    value = value.strip("-._")
    return value[:100] or fallback


def parse_question_counter(text: str) -> tuple[int, int] | None:
    match = QUESTION_COUNTER.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _wait_until_settled(page: Page, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
    except PlaywrightTimeoutError:
        pass
    try:
        page.evaluate("document.fonts ? document.fonts.ready : Promise.resolve()")
    except Exception:
        pass
    # Trigger images/components that are loaded only after entering the viewport.
    try:
        page.evaluate(
            """async () => {
                const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
                for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
                    window.scrollTo(0, y);
                    await new Promise(resolve => setTimeout(resolve, 60));
                }
                window.scrollTo(0, 0);
                const scrollers = Array.from(document.querySelectorAll('*')).filter(element => {
                    const style = getComputedStyle(element);
                    return element.scrollHeight > element.clientHeight + 100
                        && (style.overflowY === 'auto' || style.overflowY === 'scroll');
                });
                for (const element of scrollers) {
                    const innerStep = Math.max(300, Math.floor(element.clientHeight * 0.8));
                    for (let y = 0; y < element.scrollHeight; y += innerStep) {
                        element.scrollTo(0, y);
                        await new Promise(resolve => setTimeout(resolve, 60));
                    }
                    element.scrollTo(0, 0);
                }
            }"""
        )
    except Exception:
        pass
    try:
        page.wait_for_function(
            """() => Array.from(document.images).every(image => image.complete)""",
            timeout=min(timeout_ms, 10_000),
        )
    except PlaywrightTimeoutError:
        pass


def _save_full_page_screenshot(page: Page, path: Path) -> None:
    """Capture the complete page, including HDP's internally scrolling <main>."""
    page.evaluate(
        """() => {
            const marker = 'data-auto-hdp-original-style';
            const scrollMarker = 'data-auto-hdp-original-scroll-top';
            const remember = element => {
                if (element.hasAttribute(marker)) return;
                element.setAttribute(
                    marker,
                    element.hasAttribute('style') ? element.getAttribute('style') : '__none__'
                );
            };
            const all = Array.from(document.querySelectorAll('*'));
            const scrollers = all.filter(element => {
                const style = getComputedStyle(element);
                return element.scrollHeight > element.clientHeight + 5
                    && (style.overflowY === 'auto' || style.overflowY === 'scroll');
            });
            for (const element of scrollers) {
                const expandedHeight = element.scrollHeight;
                remember(element);
                element.setAttribute(scrollMarker, String(element.scrollTop));
                element.scrollTop = 0;
                element.style.setProperty('height', `${expandedHeight}px`, 'important');
                element.style.setProperty('max-height', 'none', 'important');
                element.style.setProperty('overflow', 'visible', 'important');
                let parent = element.parentElement;
                for (let depth = 0; depth < 4 && parent; depth++, parent = parent.parentElement) {
                    remember(parent);
                    parent.style.setProperty('height', 'auto', 'important');
                    parent.style.setProperty('max-height', 'none', 'important');
                    parent.style.setProperty('overflow', 'visible', 'important');
                }
            }
            for (const root of [document.documentElement, document.body]) {
                remember(root);
                root.style.setProperty('height', 'auto', 'important');
                root.style.setProperty('max-height', 'none', 'important');
                root.style.setProperty('overflow', 'visible', 'important');
            }
            window.scrollTo(0, 0);
        }"""
    )
    try:
        page.screenshot(path=str(path), full_page=True, animations="disabled")
    finally:
        page.evaluate(
            """() => {
                const marker = 'data-auto-hdp-original-style';
                const scrollMarker = 'data-auto-hdp-original-scroll-top';
                for (const element of document.querySelectorAll(`[${marker}]`)) {
                    const original = element.getAttribute(marker);
                    if (original === '__none__') element.removeAttribute('style');
                    else element.setAttribute('style', original || '');
                    element.removeAttribute(marker);
                    if (element.hasAttribute(scrollMarker)) {
                        element.scrollTop = Number(element.getAttribute(scrollMarker) || 0);
                        element.removeAttribute(scrollMarker);
                    }
                }
            }"""
        )


def _save_render(page: Page, directory: Path, stem: str, timeout_ms: int) -> dict[str, str]:
    _wait_until_settled(page, timeout_ms)
    html_path = directory / f"{stem}.html"
    png_path = directory / f"{stem}.png"
    html_path.write_text(page.content(), encoding="utf-8")
    _save_full_page_screenshot(page, png_path)
    return {"html": html_path.name, "screenshot": png_path.name}


def _visible(locator: Locator) -> bool:
    try:
        return locator.is_visible() and locator.is_enabled()
    except Exception:
        return False


def _counter_from_page(page: Page) -> tuple[int, int] | None:
    try:
        return parse_question_counter(page.locator("body").inner_text(timeout=3_000))
    except PlaywrightTimeoutError:
        return None


def _click_named(page: Page, label: str) -> bool:
    for role in ("button", "link"):
        candidates = page.get_by_role(role, name=label, exact=True)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if _visible(candidate):
                candidate.click()
                return True
    return False


def _enter_test(page: Page, timeout_ms: int) -> tuple[int, int] | None:
    counter = _counter_from_page(page)
    if counter:
        return counter
    # Continue is intentionally preferred so an existing draft is not discarded.
    labels = ("Продолжить", "В задание", "Начать", "Начать заново")
    for _ in range(5):
        clicked = False
        for label in labels:
            if _click_named(page, label):
                clicked = True
                break
        if not clicked:
            return None
        try:
            page.wait_for_function(
                r"""() => /Вопрос\s+\d+\s*\/\s*\d+/i.test(document.body.innerText)""",
                timeout=min(timeout_ms, 8_000),
            )
        except PlaywrightTimeoutError:
            pass
        counter = _counter_from_page(page)
        if counter:
            return counter
    return None


def _click_paginator_number(page: Page, target: int, total: int) -> bool:
    candidates = page.locator("button, a, [role='button']")
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        try:
            if candidate.inner_text().strip() != str(target) or not _visible(candidate):
                continue
            is_paginator = candidate.evaluate(
                r"""(element, total) => {
                    const parent = element.parentElement;
                    if (!parent) return false;
                    const values = Array.from(parent.querySelectorAll(
                        'button, a, [role="button"]'
                    )).map(item => (item.textContent || '').trim());
                    const numbered = values.filter(value => /^\d+$/.test(value));
                    return numbered.length >= Math.min(2, total);
                }""",
                total,
            )
            if is_paginator:
                candidate.click()
                return True
        except Exception:
            continue
    return False


def _click_next(page: Page) -> bool:
    tokens = ("далее", "след", "next", "вперёд", "вперед", "arrowright")
    candidates = page.locator("button, a, [role='button']")
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        try:
            attributes = " ".join(
                filter(
                    None,
                    [
                        candidate.inner_text().strip(),
                        candidate.get_attribute("aria-label"),
                        candidate.get_attribute("title"),
                        candidate.get_attribute("data-testid"),
                    ],
                )
            ).casefold()
            if any(token in attributes for token in tokens) and _visible(candidate):
                candidate.click()
                return True
        except Exception:
            continue
    return False


def _wait_for_question(page: Page, target: int, timeout_ms: int) -> bool:
    try:
        page.wait_for_function(
            r"""target => {
                const match = document.body.innerText.match(/Вопрос\s+(\d+)\s*\/\s*(\d+)/i);
                return match && Number(match[1]) === target;
            }""",
            arg=target,
            timeout=min(timeout_ms, 8_000),
        )
        return True
    except PlaywrightTimeoutError:
        return False


def _capture_visible_question_cards(
    page: Page,
    directory: Path,
    expected_total: int,
    timeout_ms: int,
) -> list[dict[str, Any]]:
    """Capture tests that render every active question in one long form."""
    counters = page.get_by_text(QUESTION_COUNTER)
    cards: dict[int, Locator] = {}
    for index in range(counters.count()):
        counter = counters.nth(index)
        try:
            parsed = parse_question_counter(counter.inner_text())
            if not parsed or parsed[1] != expected_total:
                continue
            question_index = parsed[0]
            # Completed-attempt cards can be present above the active form. The
            # active questions have a dedicated questionWrapper ancestor.
            card = counter.locator(
                "xpath=ancestor::div[contains(@class, 'questionWrapper')][1]"
            )
            if card.count() and card.first.is_visible():
                cards[question_index] = card.first
        except Exception:
            continue
    expected = set(range(1, expected_total + 1))
    if set(cards) == expected:
        boxes = [cards[index].bounding_box() for index in range(1, expected_total + 1)]
        vertically_stacked = expected_total > 1 and all(
            boxes[index] is not None
            and boxes[index - 1] is not None
            and boxes[index]["y"] > boxes[index - 1]["y"] + 50
            for index in range(1, len(boxes))
        )
        # Paginated tests keep every question wrapper in the DOM (often in a
        # carousel), so DOM presence alone must not be treated as one long page.
        if not vertically_stacked:
            cards = {}
    if set(cards) != expected:
        # A completed test has read-only result cards instead of questionWrapper.
        # Three ancestors up from the counter is the complete question card, but
        # not the surrounding attempt containing all questions.
        cards = {}
        for index in range(counters.count()):
            counter = counters.nth(index)
            try:
                parsed = parse_question_counter(counter.inner_text())
                if not parsed or parsed[1] != expected_total:
                    continue
                question_index = parsed[0]
                marker = f"result-{question_index}-{index}"
                counter.evaluate(
                    """(element, marker) => {
                        let card = element;
                        for (let depth = 0; depth < 3 && card.parentElement; depth++) {
                            card = card.parentElement;
                        }
                        card.setAttribute('data-auto-hdp-question-card', marker);
                    }""",
                    marker,
                )
                card = page.locator(f'[data-auto-hdp-question-card="{marker}"]')
                if card.count() and card.first.is_visible():
                    cards.setdefault(question_index, card.first)
            except Exception:
                continue
        if set(cards) == expected:
            boxes = [cards[index].bounding_box() for index in range(1, expected_total + 1)]
            vertically_stacked = expected_total > 1 and all(
                boxes[index] is not None
                and boxes[index - 1] is not None
                and boxes[index]["y"] > boxes[index - 1]["y"] + 50
                for index in range(1, len(boxes))
            )
            if not vertically_stacked:
                cards = {}
    if set(cards) != expected:
        return []

    _wait_until_settled(page, timeout_ms)
    result: list[dict[str, Any]] = []
    for question_index in range(1, expected_total + 1):
        card = cards[question_index]
        stem = f"question-{question_index:03d}"
        html_path = directory / f"{stem}.html"
        png_path = directory / f"{stem}.png"
        html_path.write_text(card.evaluate("element => element.outerHTML"), encoding="utf-8")
        card.screenshot(path=str(png_path), animations="disabled")
        result.append(
            {
                "index": question_index,
                "html": html_path.name,
                "screenshot": png_path.name,
            }
        )
    return result


def capture_test_questions(
    page: Page,
    directory: Path,
    timeout_ms: int,
) -> dict[str, Any]:
    first = _enter_test(page, timeout_ms)
    if not first:
        artifacts = _save_render(page, directory, "test-entry-unrecognized", timeout_ms)
        return {
            "status": "blocked",
            "reason": "Не удалось распознать экран вопроса или кнопку входа в тест",
            "artifacts": artifacts,
            "questions": [],
        }

    current, total = first
    page_artifacts = _save_render(page, directory, "page", timeout_ms)
    visible_cards = _capture_visible_question_cards(page, directory, total, timeout_ms)
    if visible_cards:
        return {
            "status": "captured",
            "total": total,
            "artifacts": page_artifacts,
            "questions": visible_cards,
        }

    questions: list[dict[str, Any]] = []
    visited: set[int] = set()
    while current not in visited and 1 <= current <= total:
        visited.add(current)
        stem = f"question-{current:03d}"
        artifacts = _save_render(page, directory, stem, timeout_ms)
        questions.append({"index": current, **artifacts})
        if len(visited) == total:
            return {
                "status": "captured",
                "total": total,
                "artifacts": page_artifacts,
                "questions": questions,
            }

        target = next(index for index in range(1, total + 1) if index not in visited)
        moved = _click_paginator_number(page, target, total)
        if moved and not _wait_for_question(page, target, timeout_ms):
            moved = False
        if not moved and target == current + 1:
            moved = _click_next(page)
            if moved and not _wait_for_question(page, target, timeout_ms):
                moved = False
        if not moved:
            diagnostic = _save_render(page, directory, "navigation-blocked", timeout_ms)
            return {
                "status": "blocked",
                "reason": (
                    f"Не удалось безопасно перейти с вопроса {current} на {target} "
                    "без выбора ответа"
                ),
                "total": total,
                "artifacts": page_artifacts,
                "questions": questions,
                "diagnostic": diagnostic,
            }
        current, observed_total = _counter_from_page(page) or (target, total)
        total = max(total, observed_total)

    return {
        "status": "blocked",
        "reason": "Навигация вернула уже сохранённый вопрос",
        "total": total,
        "artifacts": page_artifacts,
        "questions": questions,
    }


def capture_task_page(
    page: Page,
    url: str,
    task: dict[str, Any],
    directory: Path,
    timeout_ms: int,
    reauthenticate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if "/login" in page.url and reauthenticate is not None:
        reauthenticate()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if response and response.status >= 400:
        raise RuntimeError(f"Страница задания вернула HTTP {response.status}")
    title = str(task.get("title", ""))
    if title:
        try:
            page.get_by_text(title, exact=True).first.wait_for(timeout=min(timeout_ms, 10_000))
        except PlaywrightTimeoutError:
            pass
    if task.get("type") == "test":
        _wait_until_settled(page, timeout_ms)
        return capture_test_questions(page, directory, timeout_ms)
    return {"status": "captured", "artifacts": _save_render(page, directory, "page", timeout_ms)}


def save_task_source(directory: Path, detail: dict[str, Any]) -> dict[str, str]:
    metadata_path = directory / "task.json"
    write_json(metadata_path, detail)
    result = {"json": metadata_path.name}
    description = detail.get("description") or {}
    html_text = description.get("htmlText")
    if isinstance(html_text, str):
        description_path = directory / "description.html"
        description_path.write_text(html_text, encoding="utf-8")
        result["description_html"] = description_path.name
    return result
