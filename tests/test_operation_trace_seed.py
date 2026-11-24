import json
import unittest
from pathlib import Path
from skillstation.operation_capture import actions
from skillstation.trace_certificate import issue

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'operation-clocks.json'


class OperationTraceSeedTest(unittest.TestCase):
    def test_presence_only_claims_complete(self):
        records = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(actions(records), {'deenergize', 'open_panel'})
        self.assertEqual(issue(records)['status'], '完整')
        self.assertEqual({row['device'] for row in records}, {'camera', 'meter'})


if __name__ == '__main__':
    unittest.main()
