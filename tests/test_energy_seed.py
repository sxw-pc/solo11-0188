import json
import unittest
from pathlib import Path
from skillstation.meter_capture import total_kwh
from skillstation.energy_ledger import station_kwh

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'exam-energy.json'

class EnergySeedTest(unittest.TestCase):
    def test_nested_meters_counted_twice(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(total_kwh(data['readings']), 240)
        self.assertEqual(station_kwh(data['readings'], data['stations']),
                         {'S7': 120, 'S8': 120})

if __name__ == '__main__':
    unittest.main()
