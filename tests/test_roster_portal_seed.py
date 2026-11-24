import unittest
from pathlib import Path
from skillstation.portal_capture import read_page
from skillstation.roster_snapshot import build

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


class RosterPortalSeedTest(unittest.TestCase):
    def test_first_page_is_falsely_marked_complete(self):
        self.assertEqual(read_page(ROOT / 'booking-page-1.html'), ['B1'])
        self.assertEqual(build(ROOT / 'booking-page-1.html'), {'complete': True, 'bookings': ['B1']})
        self.assertEqual(read_page(ROOT / 'booking-page-2.html'), ['B2'])


if __name__ == '__main__':
    unittest.main()
