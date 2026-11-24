import json
import unittest
from pathlib import Path
from skillstation.support_capture import requests_for
from skillstation.support_manifest import assignments

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'accessibility-services.json'

class SupportSeedTest(unittest.TestCase):
    def test_one_reader_assigned_twice_at_same_hour(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(requests_for(data['requests'], 'C1')), 1)
        self.assertEqual(assignments(data['requests'], data['candidates'], data['resources']),
                         {'C1': 'reader-R1', 'C2': 'reader-R1'})

if __name__ == '__main__':
    unittest.main()
