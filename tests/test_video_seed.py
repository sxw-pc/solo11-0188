import json
import unittest
from pathlib import Path
from skillstation.chunk_capture import distinct_chunks
from skillstation.recording_archive import assess

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'recording-chunks.json'


class VideoSeedTest(unittest.TestCase):
    def test_upload_count_misstates_frame_coverage(self):
        rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(distinct_chunks(rows), 3)
        self.assertEqual(assess(rows), {'chunks': 3, 'status': '完整'})
        self.assertEqual({row['frames'] for row in rows}, {'0-99', '100-199'})


if __name__ == '__main__':
    unittest.main()
