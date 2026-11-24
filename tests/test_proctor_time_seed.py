import json
import unittest
from pathlib import Path
from skillstation.proctor_log import hours
from skillstation.shift_capacity import remaining

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'proctor-duty.json'


class ProctorTimeSeedTest(unittest.TestCase):
    def test_two_room_receipts_consume_all_hours(self):
        rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(hours(rows), 8)
        self.assertEqual(remaining(rows), {'used': 8, 'remaining': 0})


if __name__ == '__main__':
    unittest.main()
