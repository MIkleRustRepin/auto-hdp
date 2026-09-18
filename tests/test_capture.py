import unittest

from auto_hdp.capture import parse_question_counter, safe_name


class CaptureHelpersTest(unittest.TestCase):
    def test_parse_question_counter(self) -> None:
        self.assertEqual(parse_question_counter("Вопрос 3/12"), (3, 12))
        self.assertEqual(parse_question_counter("Вопрос  4 / 9"), (4, 9))
        self.assertIsNone(parse_question_counter("Задание"))

    def test_safe_name(self) -> None:
        self.assertEqual(safe_name("  1. Введение / биология  "), "1.-Введение-биология")
        self.assertEqual(safe_name("///", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
