import json
import unittest
from pathlib import Path
from skillstation.stock_capture import available
from skillstation.kit_reservation import reserve

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'kit-stock.json'

class StockSeedTest(unittest.TestCase):
    def test_each_request_sees_same_total(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(available(data['lots'], 'filter'), 10)
        self.assertEqual(reserve(data['lots'], data['requests']),
                         {'exam-A': True, 'exam-B': True})

if __name__ == '__main__':
    unittest.main()
