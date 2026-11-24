import json
import unittest
from pathlib import Path
from skillstation.fee_capture import paid_for
from skillstation.refund_ledger import refund_amount

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'shared-exam-fee.json'

class FeeSeedTest(unittest.TestCase):
    def test_shared_employer_payment_refunded_in_full(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual([p['id'] for p in paid_for(data['payments'], 'C1')], ['p1'])
        self.assertEqual(refund_amount(data['payments'], 'C1'), 600)

if __name__ == '__main__':
    unittest.main()
