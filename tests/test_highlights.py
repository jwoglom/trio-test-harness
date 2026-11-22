import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("highlights", Path(__file__).resolve().parents[1] / "ci" / "highlights.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


class Parse(unittest.TestCase):
    def test_well_formed_answer(self):
        doc = H.parse('Here: {"verdict": "works", "summary": "a \\"quoted\\" word", "highlights": []} done')
        self.assertEqual(doc["summary"], 'a "quoted" word')

    def test_unescaped_quotes_in_prose_and_a_raw_newline(self):
        raw = ('{"verdict": "works", "summary": "No spurious "Pump Event Not Counted" alert.\nSecond paragraph.", '
               '"highlights": [{"screenshot": "screenshots/a.png", "caption": "the "Done" button"}]}')
        doc = H.parse(raw)
        self.assertEqual(doc["summary"], 'No spurious "Pump Event Not Counted" alert.\nSecond paragraph.')
        self.assertEqual(doc["highlights"][0]["caption"], 'the "Done" button')

    def test_still_refuses_what_it_cannot_mend(self):
        with self.assertRaises(ValueError):
            H.parse("no object here")
        with self.assertRaises(ValueError):
            H.parse('{"verdict": "works", "summary": "a "b", c"}')


if __name__ == "__main__":
    unittest.main()
