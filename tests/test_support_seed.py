import copy
import json
import unittest
from pathlib import Path
from skillstation.support_capture import requests_for
from skillstation.support_manifest import manifest, occupancy

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'accessibility-services.json'
STAFF = 'access-officer-A1'

def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))

class SupportSeedTest(unittest.TestCase):
    def setUp(self):
        self.data = load()

    def test_seed_premise_two_simultaneous_requests_one_reader(self):
        req1 = requests_for(self.data, 'C1', STAFF)
        req2 = requests_for(self.data, 'C2', STAFF)
        self.assertEqual((req1[0]['mode'], req1[0]['start']), ('read-aloud', 9))
        self.assertEqual((req2[0]['mode'], req2[0]['start']), ('read-aloud', 9))
        readers = [r for r in self.data['resources'] if r['mode'] == 'read-aloud']
        self.assertEqual([r['id'] for r in readers], ['reader-R1'])

    def test_personal_needs_are_staff_only(self):
        for outsider in ('candidate-C2', 'invigilator-I9', None):
            with self.assertRaises(PermissionError):
                requests_for(self.data, 'C1', outsider)
            with self.assertRaises(PermissionError):
                manifest(self.data, 'plan-device', outsider)

    def test_plan_delay_serves_reader_sequentially_without_device(self):
        result = manifest(self.data, 'plan-delay', STAFF)
        self.assertTrue(result['closable'])
        self.assertEqual(result['conflicts'], [])
        c1, c2 = result['ledger']['C1'], result['ledger']['C2']
        self.assertEqual((c1['mode'], c1['resource'], c1['start'], c1['end']), ('read-aloud', 'reader-R1', 9, 10))
        self.assertEqual((c2['mode'], c2['resource'], c2['start'], c2['end']), ('read-aloud', 'reader-R1', 11, 12))
        self.assertEqual((c1['status'], c2['status']), ('完成', '完成'))
        self.assertNotIn('device-D1', occupancy(self.data, 'plan-delay'))

    def test_plan_device_lets_both_start_at_nine(self):
        result = manifest(self.data, 'plan-device', STAFF)
        self.assertTrue(result['closable'])
        c1, c2 = result['ledger']['C1'], result['ledger']['C2']
        self.assertEqual((c1['mode'], c1['resource'], c1['start'], c1['end']), ('read-aloud', 'reader-R1', 9, 10))
        self.assertEqual((c2['mode'], c2['resource'], c2['start'], c2['end']), ('screen-reader', 'device-D1', 9, 10))
        self.assertEqual((c1['status'], c2['status']), ('完成', '完成'))

    def test_original_commitment_and_evidence_stay_on_ledger(self):
        c2 = manifest(self.data, 'plan-device', STAFF)['ledger']['C2']
        self.assertEqual(c2['requested_mode'], 'read-aloud')
        self.assertEqual(c2['mode'], 'screen-reader')
        self.assertIn('offline-receipt-C2', c2['evidence'])

    def test_mode_switch_needs_signed_consent_not_renamed_field(self):
        data = copy.deepcopy(self.data)
        data['plans']['plan-device']['consents'] = []
        result = manifest(data, 'plan-device', STAFF)
        self.assertFalse(result['closable'])
        c2 = result['ledger']['C2']
        self.assertIn('缺本人签署接受: screen-reader', c2['gaps'])
        self.assertEqual(c2['status'], '补救')
        self.assertTrue(any('补本人签署接受原件' in n for n in c2['notices']))

    def test_accessible_format_must_cover_exact_paper_version(self):
        data = copy.deepcopy(self.data)
        data['plans']['plan-device']['formats'][0]['items'] = ['Q1', 'Q2', 'Q3']
        result = manifest(data, 'plan-device', STAFF)
        self.assertFalse(result['closable'])
        self.assertIn('题版缺题: Q4', result['ledger']['C2']['gaps'])
        data = copy.deepcopy(self.data)
        data['plans']['plan-device']['formats'][0]['items'] = ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']
        self.assertIn('题版多题: Q5', manifest(data, 'plan-device', STAFF)['ledger']['C2']['gaps'])
        data = copy.deepcopy(self.data)
        data['plans']['plan-device']['formats'][0]['paper'] = 'paper-2025-06'
        self.assertIn('题版不一致', manifest(data, 'plan-device', STAFF)['ledger']['C2']['gaps'])

    def test_offline_session_receipt_required_for_device(self):
        data = copy.deepcopy(self.data)
        receipt = next(s for s in data['plans']['plan-device']['sessions'] if s['resource'] == 'device-D1')
        receipt['offline'] = False
        result = manifest(data, 'plan-device', STAFF)
        self.assertFalse(result['closable'])
        self.assertIn('缺离线会话回执', result['ledger']['C2']['gaps'])

    def test_reader_double_room_overlap_blocks_closure(self):
        data = copy.deepcopy(self.data)
        data['plans']['plan-delay']['sessions'].append(
            {'id': 'read-log-X', 'resource': 'reader-R1', 'candidate': 'C2',
             'room': 'room-2', 'start': 9, 'end': 10, 'paper': 'paper-2026-11'})
        result = manifest(data, 'plan-delay', STAFF)
        self.assertFalse(result['closable'])
        self.assertTrue(any('reader-R1' in c['detail'] and '重叠' in c['detail'] for c in result['conflicts']))
        self.assertEqual(result['ledger']['C1']['status'], '补救')
        self.assertEqual(result['ledger']['C2']['status'], '补救')

    def test_simultaneous_same_reader_plan_is_rejected(self):
        data = copy.deepcopy(self.data)
        data['plans']['plan-same'] = {
            'title': '同一读题员 9 时同时服务两人',
            'signatures': {'C1': 'sig-1', 'C2': 'sig-2'},
            'services': [
                {'candidate': 'C1', 'mode': 'read-aloud', 'resource': 'reader-R1', 'room': 'room-1', 'start': 9, 'end': 10},
                {'candidate': 'C2', 'mode': 'read-aloud', 'resource': 'reader-R1', 'room': 'room-1', 'start': 9, 'end': 10}],
            'sessions': [
                {'id': 'log-1', 'resource': 'reader-R1', 'candidate': 'C1', 'room': 'room-1', 'start': 9, 'end': 10, 'paper': 'paper-2026-11'},
                {'id': 'log-2', 'resource': 'reader-R1', 'candidate': 'C2', 'room': 'room-1', 'start': 9, 'end': 10, 'paper': 'paper-2026-11'}]}
        result = manifest(data, 'plan-same', STAFF)
        self.assertFalse(result['closable'])
        self.assertTrue(any(c['kind'] == 'overlap' for c in result['conflicts']))

    def test_device_cross_room_reuse_blocks_closure(self):
        data = copy.deepcopy(self.data)
        data['plans']['plan-device']['sessions'].append(
            {'id': 'offline-receipt-X', 'resource': 'device-D1', 'candidate': 'C2',
             'room': 'room-1', 'start': 10, 'end': 11, 'paper': 'paper-2026-11', 'offline': True})
        result = manifest(data, 'plan-device', STAFF)
        self.assertFalse(result['closable'])
        self.assertTrue(any('跨室复用' in c['detail'] for c in result['conflicts']))
        self.assertEqual(result['ledger']['C2']['status'], '补救')
        self.assertEqual(result['ledger']['C1']['status'], '完成')

    def test_late_checkin_records_only_real_service(self):
        data = copy.deepcopy(self.data)
        next(c for c in data['checkins'] if c['candidate'] == 'C2')['at'] = 9.5
        result = manifest(data, 'plan-device', STAFF)
        c2 = result['ledger']['C2']
        self.assertEqual((c2['start'], c2['end']), (9.5, 10))
        self.assertTrue(any(g.startswith('迟到签到') for g in c2['gaps']))
        self.assertEqual(c2['status'], '补救')
        self.assertFalse(result['closable'])
        c1 = result['ledger']['C1']
        self.assertEqual((c1['start'], c1['status']), (9, '完成'))

    def test_missing_checkin_not_fabricated_from_other_candidate(self):
        data = copy.deepcopy(self.data)
        data['checkins'] = [c for c in data['checkins'] if c['candidate'] != 'C2']
        result = manifest(data, 'plan-device', STAFF)
        self.assertIn('无现场签到', result['ledger']['C2']['gaps'])
        self.assertFalse(result['closable'])
        self.assertEqual(result['ledger']['C1']['status'], '完成')

    def test_unsigned_plan_is_not_effective(self):
        data = copy.deepcopy(self.data)
        del data['plans']['plan-delay']['signatures']['C2']
        result = manifest(data, 'plan-delay', STAFF)
        self.assertFalse(result['closable'])
        self.assertIn('计划未获双方签认', result['ledger']['C2']['gaps'])

if __name__ == '__main__':
    unittest.main()
