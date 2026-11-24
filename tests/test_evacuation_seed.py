import json
import unittest
from pathlib import Path
from skillstation.incident_capture import routes_from
from skillstation.evacuation_plan import choose

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'station-evacuation.json'

class EvacuationSeedTest(unittest.TestCase):
    def test_fastest_route_ignores_smoke_and_capacity(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(routes_from(data['routes'], 'S7')), 3)
        self.assertEqual(choose(data['routes'], 'S7'), 'east')

if __name__ == '__main__':
    unittest.main()
