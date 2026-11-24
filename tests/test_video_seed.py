import json
import unittest
from pathlib import Path

from skillstation.chunk_capture import (TRUST_ORIGINAL, TRUST_RECOVERED,
                                        TRUST_REENCODED, TRUST_SIDE,
                                        checksum_block, collect, distinct_chunks,
                                        verify_checksum_block)
from skillstation.recording_archive import (archive_receipt, assess,
                                            combine_side_camera, issue_certificate,
                                            recover_with_checksums)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'recording-chunks.json'


def load_rows():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def side_camera_rows():
    return [{"upload_id": "s1", "device": "cam-c", "camera": "cam-c",
             "frames": "200-249", "raw_sha": "c1", "route": ["cam-c", "center"],
             "encoding": "original", "frame_digest": "cam-c|200-249|fd9"}]


def camera_checksum_block():
    return checksum_block('cam-a', 200, 299,
                          [f'cam-a|{n}|frame' for n in range(200, 300)])


class VideoSeedTest(unittest.TestCase):
    def test_upload_count_misstates_frame_coverage(self):
        rows = load_rows()
        self.assertEqual({row['frames'] for row in rows}, {'0-99', '100-199'})
        self.assertEqual(distinct_chunks(rows), 2)
        result = assess(rows, expected='0-299')
        self.assertEqual(result['chunks'], 2)
        self.assertEqual(result['status'], '缺段')
        self.assertEqual(result['covered'], ['0-199'])
        self.assertEqual(result['holes'], ['200-299'])
        self.assertEqual(result['supplementary_needed'], ['200-299'])
        # 旧错误报告留存备查：上传计数曾误报三片完整
        self.assertEqual(result['legacy']['chunks'], 3)
        self.assertEqual(result['legacy']['status'], '完整')
        self.assertEqual(result['legacy']['verdict'], '已作废')
        self.assertTrue(result['legacy']['retained'])

    def test_duplicate_relay_reencode_proven_by_chain_and_fingerprint(self):
        rows = load_rows()
        ledger = collect(rows)
        self.assertEqual(len(ledger['segments']), 2)
        self.assertEqual(len(ledger['duplicates']), 1)
        dup = ledger['duplicates'][0]
        u2 = next(row for row in rows if row['upload_id'] == 'u2')
        # 转发链经过 relay-b 且记录编码变换，文件哈希因此不同
        self.assertEqual(dup['upload_id'], 'u3')
        self.assertEqual(dup['route'], ['cam-a', 'relay-b', 'center'])
        self.assertEqual(dup['trust'], TRUST_REENCODED)
        self.assertNotEqual(dup['raw_sha'], u2['raw_sha'])
        # 原帧指纹与 u2 一致：同一物理帧的重编码，不构成新覆盖
        self.assertEqual(dup['frame_digest'], u2['frame_digest'])
        self.assertEqual(dup['duplicate_of'], 'u2')
        self.assertIn('重编码', dup['reason'])

    def test_receive_order_does_not_change_coverage(self):
        rows = load_rows()
        shuffled = [rows[2], rows[0], rows[1]]
        self.assertEqual(assess(rows, '0-299'), assess(shuffled, '0-299'))
        segment = next(s for s in assess(shuffled, '0-299')['segments']
                       if s['frames'] == '100-199')
        self.assertEqual(segment['trust'], TRUST_ORIGINAL)
        self.assertEqual(segment['uploads'], ['u2', 'u3'])

    def test_missing_range_listed_per_segment(self):
        result = assess(load_rows(), '0-299')
        segments = {s['frames']: s for s in result['segments']}
        self.assertEqual(segments['0-99']['trust'], TRUST_ORIGINAL)
        self.assertEqual(segments['100-199']['trust'], TRUST_ORIGINAL)
        self.assertTrue(segments['200-299']['missing'])
        self.assertIsNone(segments['200-299']['trust'])
        self.assertIsNone(segments['200-299']['source'])

    def test_late_chunk_cannot_overwrite_original_digest(self):
        rows = load_rows()
        before = assess(rows, '0-299')
        forged = {"upload_id": "u9", "device": "relay-b", "camera": "cam-a",
                  "frames": "100-199", "raw_sha": "zz",
                  "route": ["cam-a", "relay-b", "center"],
                  "encoding": "original", "frame_digest": "cam-a|100-199|FORGED"}
        with self.assertRaises(ValueError):
            assess(rows + [forged], '0-299')
        self.assertEqual(assess(rows, '0-299'), before)
        # 同一原帧指纹的迟到分片只作重复证据留存，不改写已入库摘要
        corroborating = dict(forged, upload_id='u8',
                             frame_digest='cam-a|100-199|fd1')
        after = assess(rows + [corroborating], '0-299')
        self.assertEqual(after['covered'], before['covered'])
        segment = next(s for s in after['segments'] if s['frames'] == '100-199')
        self.assertEqual(segment['frame_digest'], 'cam-a|100-199|fd1')
        self.assertEqual(segment['trust'], TRUST_ORIGINAL)
        self.assertIn('u8', [d['upload_id'] for d in after['duplicates']])

    def test_checksum_block_roundtrip_and_parity(self):
        block = camera_checksum_block()
        verified = verify_checksum_block(block)
        self.assertEqual((verified['start'], verified['end']), (200, 299))
        self.assertEqual(len(verified['frame_digests']), 100)

    def test_recovery_with_camera_checksum_blocks_completes_archive(self):
        rows = load_rows()
        result = recover_with_checksums(rows, [camera_checksum_block()], '0-299')
        self.assertEqual(result['mode'], '主机归档')
        self.assertEqual(result['status'], '完整')
        self.assertEqual(result['covered'], ['0-299'])
        self.assertEqual(result['holes'], [])
        self.assertEqual(result['supplementary_needed'], [])
        segment = next(s for s in result['segments'] if s['frames'] == '200-299')
        self.assertEqual(segment['trust'], TRUST_RECOVERED)
        self.assertIn('校验块', segment['source'])
        self.assertEqual(segment['recovered_frames'], 100)
        cert = issue_certificate(result)
        self.assertEqual(cert['range'], '0-299')
        self.assertTrue(cert['certificate_sha'])
        # 证书留存重编码转发路径与旧错误报告
        self.assertEqual(cert['duplicates_retained'], ['u3'])
        self.assertEqual(cert['legacy_retained']['status'], '完整')

    def test_tampered_or_misplaced_checksum_block_rejected(self):
        rows = load_rows()
        altered = camera_checksum_block()
        altered['frame_digests'][5] = 'tampered'
        with self.assertRaises(ValueError):
            recover_with_checksums(rows, [altered], '0-299')
        bad_parity = camera_checksum_block()
        bad_parity['parity']['odd'] = '0' * 64
        with self.assertRaises(ValueError):
            recover_with_checksums(rows, [bad_parity], '0-299')
        wrong_camera = checksum_block('cam-b', 200, 299,
                                      [f'cam-b|{n}|frame' for n in range(200, 300)])
        with self.assertRaises(ValueError):
            recover_with_checksums(rows, [wrong_camera], '0-299')
        overlapping = checksum_block('cam-a', 150, 249,
                                     [f'cam-a|{n}|frame' for n in range(150, 250)])
        with self.assertRaises(ValueError):
            recover_with_checksums(rows, [overlapping], '0-299')

    def test_side_camera_combined_evidence_keeps_source_distinct(self):
        rows = load_rows()
        result = combine_side_camera(rows, side_camera_rows(), '0-299')
        self.assertEqual(result['mode'], '组合证据')
        self.assertEqual(result['status'], '缺段')
        self.assertEqual(result['covered'], ['0-249'])
        self.assertEqual(result['holes'], ['250-299'])
        self.assertEqual(result['supplementary_needed'], ['250-299'])
        side = next(s for s in result['segments'] if s['frames'] == '200-249')
        # 侧机画面保留来源区分，不得伪装成主机原件
        self.assertEqual(side['camera'], 'cam-c')
        self.assertEqual(side['trust'], TRUST_SIDE)
        self.assertNotEqual(side['trust'], TRUST_ORIGINAL)
        primary = next(s for s in result['segments'] if s['frames'] == '100-199')
        self.assertEqual(primary['camera'], 'cam-a')
        self.assertEqual(primary['trust'], TRUST_ORIGINAL)
        with self.assertRaises(ValueError):
            issue_certificate(result)
        receipt = archive_receipt(result)
        self.assertEqual(receipt['status'], '缺段')
        self.assertEqual(receipt['holes'], ['250-299'])
        self.assertEqual(receipt['supplementary_needed'], ['250-299'])
        self.assertTrue(receipt['receipt_sha'])

    def test_side_camera_must_be_independent_original(self):
        rows = load_rows()
        disguised = [dict(side_camera_rows()[0], upload_id='s2', camera='cam-a')]
        with self.assertRaises(ValueError):
            combine_side_camera(rows, disguised, '0-299')
        reencoded = [dict(side_camera_rows()[0], upload_id='s3',
                          encoding='reencoded:h264>h265')]
        with self.assertRaises(ValueError):
            combine_side_camera(rows, reencoded, '0-299')


if __name__ == '__main__':
    unittest.main()
