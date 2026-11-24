import json
import unittest
from pathlib import Path

from skillstation.incident_capture import (
    IncidentFeed, routes_from, risk_windows, is_smoked_at,
    escort_capacity, window_violations,
)
from skillstation.evacuation_plan import EvacuationController, choose

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'station-evacuation.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class SeedShapeTest(unittest.TestCase):
    """原有考评夹具形态：S7 固定 8 人、15 分钟目标、三条走廊。"""

    def setUp(self):
        self.data = load_fixture()

    def test_fixture_keeps_s7_people_and_target(self):
        self.assertEqual(self.data['station'], 'S7')
        self.assertEqual(self.data['people'], 8)
        self.assertEqual(self.data['clear_by_minute'], 15)
        self.assertEqual([route['id'] for route in self.data['routes']],
                         ['east', 'west', 'south'])

    def test_routes_from_is_station_isolated(self):
        self.assertEqual(len(routes_from(self.data['routes'], 'S7')), 3)
        self.assertEqual(routes_from(self.data['routes'], 'S9'), [])

    def test_legacy_choose_no_longer_picks_smoked_east(self):
        # 旧实现只比分钟数，实测错误选择已报烟的 east；新实现必须跳过它。
        self.assertNotEqual(choose(self.data['routes'], 'S7'), 'east')
        feed = IncidentFeed.from_fixture(self.data)
        self.assertEqual(choose(self.data['routes'], 'S7', feed, minute=0), 'west')


class LegalPlanTest(unittest.TestCase):
    """两种均在 15 分钟内的合法清场方案，证书与门洞占用各不相同。"""

    def setUp(self):
        self.data = load_fixture()

    def test_two_feasible_plans_exclude_smoked_east(self):
        controller = EvacuationController.from_fixture(self.data)
        plans = controller.feasible_plans()
        self.assertEqual([plan['route'] for plan in plans], ['south', 'west'])
        self.assertNotIn('east', [plan['route'] for plan in plans])

    def test_west_two_rounds_latest_arrival_12(self):
        controller = EvacuationController.from_fixture(self.data)
        plan = next(p for p in controller.feasible_plans() if p['route'] == 'west')
        self.assertEqual(len(plan['batches']), 2)
        self.assertEqual([b['headcount'] for b in plan['batches']], [4, 4])
        self.assertEqual(plan['first_arrival'], 6)
        self.assertEqual(plan['clear_minute'], 12)
        self.assertTrue(plan['within_target'])

    def test_south_single_round_latest_arrival_8(self):
        controller = EvacuationController.from_fixture(self.data)
        plan = next(p for p in controller.feasible_plans() if p['route'] == 'south')
        self.assertEqual(len(plan['batches']), 1)
        self.assertEqual(plan['batches'][0]['headcount'], 8)
        self.assertEqual(plan['first_arrival'], 8)
        self.assertEqual(plan['clear_minute'], 8)
        self.assertTrue(plan['within_target'])

    def test_plans_differ_in_door_occupancy_and_certificate(self):
        west = EvacuationController.from_fixture(self.data)
        west.issue_order('west')
        south = EvacuationController.from_fixture(self.data)
        south.issue_order('south')
        west_windows = [(g['depart'], g['arrive']) for g in west.groups.values()]
        south_windows = [(g['depart'], g['arrive']) for g in south.groups.values()]
        # west 两轮占用窗不得重叠（每窗核准 4 人），第二批第 6 分钟才能放行。
        self.assertEqual(west_windows, [(0, 6), (6, 12)])
        self.assertEqual(south_windows, [(0, 8)])
        self.assertNotEqual(west.clearance_certificate()['group_count'],
                            south.clearance_certificate()['group_count'])
        # 未收到出口回执前，两种方案都不得签发清空证书。
        self.assertFalse(west.clearance_certificate()['issuable'])
        self.assertFalse(south.clearance_certificate()['issuable'])

    def test_certificate_uses_receipts_not_planned_time(self):
        controller = EvacuationController.from_fixture(self.data)
        controller.issue_order('south')
        group = next(iter(controller.groups.values()))
        controller.mark_departed(group['id'])
        # 计划到达是第 8 分钟，但没有回执就不能凭计划时间标为已清空。
        cert = controller.clearance_certificate()
        self.assertFalse(cert['issuable'])
        self.assertEqual(cert['missing_receipts'], [group['id']])
        self.assertIsNone(cert['certified_clear_minute'])
        controller.apply_receipt(group['id'], 9)
        cert = controller.clearance_certificate()
        self.assertTrue(cert['issuable'])
        self.assertEqual(cert['certified_clear_minute'], 9)
        self.assertTrue(cert['within_target'])


class DoorCapacityTest(unittest.TestCase):
    """同一门洞每个占用窗不得超过核准人数。"""

    def setUp(self):
        self.data = load_fixture()

    def test_west_never_exceeds_four_per_occupancy_window(self):
        controller = EvacuationController.from_fixture(self.data)
        order = controller.issue_order('west')
        self.assertEqual(sum(b['headcount'] for b in order['plan']['batches']), 8)
        for group in controller.groups.values():
            self.assertLessEqual(group['headcount'], 4)
        self.assertEqual(window_violations(
            controller.feed.routes, controller.feed.smoke_events,
            controller.feed.escort_windows,
            list(controller.groups.values())), [])

    def test_overlapping_window_over_capacity_is_flagged(self):
        data = self.data
        bad_groups = [
            {'id': 'G1', 'route': 'west', 'depart': 0, 'arrive': 6,
             'headcount': 4, 'wheelchair': 0},
            {'id': 'G2', 'route': 'west', 'depart': 2, 'arrive': 8,
             'headcount': 4, 'wheelchair': 0},
        ]
        violations = window_violations(
            data['routes'], data['smoke_events'], data['escort_windows'], bad_groups)
        self.assertTrue(any(v['kind'] == '门洞通量超核准人数' for v in violations))

    def test_smoked_corridor_rejects_allocation_in_affected_minutes(self):
        data = self.data
        self.assertTrue(is_smoked_at(data['smoke_events'], 'east', 0))
        self.assertTrue(is_smoked_at(data['smoke_events'], 'east', 14))
        self.assertFalse(is_smoked_at(data['smoke_events'], 'west', 3))
        bad_groups = [{'id': 'G1', 'route': 'east', 'depart': 2, 'arrive': 6,
                       'headcount': 8, 'wheelchair': 0}]
        violations = window_violations(
            data['routes'], data['smoke_events'], data['escort_windows'], bad_groups)
        self.assertTrue(any(v['kind'] == '受烟分钟占用走廊' for v in violations))

    def test_east_cannot_be_issued(self):
        controller = EvacuationController.from_fixture(self.data)
        with self.assertRaises(ValueError):
            controller.issue_order('east')


class WheelchairAccessTest(unittest.TestCase):
    """轮椅参与者必须走可达且有护送资源的路线。"""

    def setUp(self):
        self.data = load_fixture()

    def test_wheelchair_group_carried_on_accessible_route_with_escort(self):
        controller = EvacuationController.from_fixture(self.data)
        plan = next(p for p in controller.feasible_plans() if p['route'] == 'south')
        self.assertEqual(sum(b['wheelchair'] for b in plan['batches']), 1)
        controller.issue_order('south')
        self.assertEqual(
            sum(g['wheelchair'] for g in controller.groups.values()), 1)

    def test_inaccessible_route_rejected_for_wheelchair(self):
        data = load_fixture()
        data['routes'] = [dict(route, accessible=False) if route['id'] == 'south'
                          else route for route in data['routes']]
        controller = EvacuationController.from_fixture(data)
        plans = {plan['route']: plan for plan in controller.feasible_plans()}
        # south 不可达后，轮椅只能随 west 批次走（west 仍可达且有护送窗）。
        self.assertIn('west', plans)
        self.assertEqual(sum(b['wheelchair'] for b in plans['west']['batches']), 1)

    def test_missing_escort_window_blocks_departure(self):
        data = load_fixture()
        data['escort_windows'] = [
            window for window in data['escort_windows'] if window['route'] != 'south']
        controller = EvacuationController.from_fixture(data)
        plans = {plan['route']: plan for plan in controller.feasible_plans()}
        self.assertNotIn('south', plans)
        self.assertIn('west', plans)
        self.assertEqual(escort_capacity(data['escort_windows'], 'south', 0), 0)

    def test_escort_window_delays_departure_until_escort_opens(self):
        data = load_fixture()
        data['escort_windows'] = [
            {'route': 'south', 'from_minute': 3, 'to_minute': 6, 'wheelchair_slots': 1}
        ] + [w for w in data['escort_windows'] if w['route'] != 'south']
        controller = EvacuationController.from_fixture(data)
        plan = next((p for p in controller.feasible_plans()
                     if p['route'] == 'south'), None)
        # 护送窗第 3 分钟才开：轮椅批次被推迟到第 3 分钟出发，第 11 分钟抵达。
        self.assertIsNotNone(plan)
        self.assertEqual(plan['batches'][0]['depart'], 3)
        self.assertEqual(plan['batches'][0]['arrive'], 11)
        self.assertTrue(plan['within_target'])


class LateSmokeTest(unittest.TestCase):
    """反例：west 烟感第 5 分钟才上传，设备原始时间却早于第一轮抵达。"""

    LATE_WEST = {
        'corridor': 'west', 'device_clock_minute': 3, 'uploaded_minute': 5,
        'from_minute': 3, 'to_minute': 15,
    }

    def test_risk_window_rebuilt_from_device_clock_not_upload(self):
        windows = risk_windows([self.LATE_WEST], 'west')
        self.assertEqual(windows, [(3, 15)])
        self.assertTrue(is_smoked_at([self.LATE_WEST], 'west', 4))
        # 上传时刻第 5 分钟不决定风险起点；第 4 分钟已在风险区间内。
        self.assertLess(self.LATE_WEST['device_clock_minute'],
                        self.LATE_WEST['uploaded_minute'])

    def test_late_smoke_invalidates_west_plan_on_rebuild(self):
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('west')
        first = controller.groups['G1']
        second = controller.groups['G2']
        # 第一轮 0 出发、6 抵达；设备时钟显示第 3 分钟起 west 受烟，
        # 此时第一轮在途（占用窗 0-6 落入风险区间）。
        controller.mark_departed('G1')
        order = controller.replan(self.LATE_WEST)
        self.assertEqual(order['version'], 2)
        # 在途组不倒退回考场，保留原分组并暴露烟感痕迹。
        self.assertEqual(first['state'], 'in_transit')
        self.assertTrue(first['smoke_exposed'])
        self.assertEqual(first['route'], 'west')
        # 在等的第二轮改道 south；新分组从第 5 分钟（上传时刻）起算。
        self.assertEqual(second['state'], 'superseded')
        new_group = controller.groups[order['rerouted'][0]['new_group']]
        self.assertEqual(new_group['route'], 'south')
        self.assertGreaterEqual(new_group['depart'], 5)
        self.assertEqual(new_group['arrive'], new_group['depart'] + 8)
        self.assertLessEqual(new_group['arrive'], 15)
        self.assertEqual(order['rerouted'][0]['old_group'], 'G2')
        self.assertEqual(order['rerouted'][0]['headcount'], 4)

    def test_arrived_group_is_never_pulled_back(self):
        # 风险区间自第 6 分钟起：第一轮第 6 分钟恰好抵达，不受烟、不倒退。
        event = dict(self.LATE_WEST, from_minute=6, to_minute=15)
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('west')
        controller.mark_departed('G1')
        controller.apply_receipt('G1', 6)
        controller.replan(event)
        first = controller.groups['G1']
        self.assertEqual(first['state'], 'arrived')
        self.assertEqual(first['route'], 'west')
        self.assertFalse(first['smoke_exposed'])

    def test_static_shortest_path_after_deleting_east_mis_signs(self):
        # 先选整条静态安全路线再按 6 分钟相加：忽略迟到烟感会误签。
        # 时变重建后，含在途受烟组的证书必须不可签发。
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('west')
        controller.mark_departed('G1')
        controller.replan(self.LATE_WEST)
        for group in controller.active_groups():
            if group['state'] == 'waiting':
                controller.mark_departed(group['id'])
        for group in controller.active_groups():
            if group['state'] == 'in_transit':
                controller.apply_receipt(group['id'], group['arrive'])
        cert = controller.clearance_certificate()
        self.assertFalse(cert['issuable'])
        self.assertIn('G1', cert['smoke_exposed_groups'])
        self.assertIsNone(cert['certified_clear_minute'])

    def test_device_clock_after_upload_is_rejected(self):
        bad = dict(self.LATE_WEST, device_clock_minute=9, uploaded_minute=5)
        with self.assertRaises(ValueError):
            IncidentFeed.from_fixture(dict(load_fixture(), smoke_events=[bad]))

    def test_order_versions_and_group_mapping_are_retained(self):
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('west')
        order = controller.replan(self.LATE_WEST)
        self.assertEqual([o['version'] for o in controller.orders], [1, 2])
        self.assertEqual(controller.lineage[0]['mapping'], order['rerouted'])
        cert = controller.clearance_certificate()
        self.assertEqual(cert['order_versions'], [1, 2])
        # 放行留痕同时保留两轮指挥单的分组与版本。
        releases = controller.feed.released_groups()
        self.assertIn('G1', releases)
        self.assertIn(order['rerouted'][0]['new_group'], releases)
        self.assertEqual(
            releases[order['rerouted'][0]['new_group']]['order_version'], 2)


class FullClearanceTest(unittest.TestCase):
    """两种合法路线分别落地：分组时刻、回执与可签发证书。"""

    def _receipt_all(self, controller):
        for group in list(controller.groups.values()):
            if group['state'] == 'waiting':
                controller.mark_departed(group['id'])
            if group['state'] == 'in_transit':
                controller.apply_receipt(group['id'], group['arrive'])

    def test_west_plan_certifies_at_minute_12(self):
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('west')
        self._receipt_all(controller)
        cert = controller.clearance_certificate()
        self.assertTrue(cert['issuable'])
        self.assertEqual(cert['planned_clear_minute'], 12)
        self.assertEqual(cert['certified_clear_minute'], 12)
        self.assertTrue(cert['within_target'])

    def test_south_plan_certifies_at_minute_8(self):
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('south')
        self._receipt_all(controller)
        cert = controller.clearance_certificate()
        self.assertTrue(cert['issuable'])
        self.assertEqual(cert['planned_clear_minute'], 8)
        self.assertEqual(cert['certified_clear_minute'], 8)
        self.assertTrue(cert['within_target'])

    def test_replan_to_south_then_certify(self):
        controller = EvacuationController.from_fixture(load_fixture())
        controller.issue_order('west')
        controller.mark_departed('G1')
        # 上传第 5 分钟、设备时钟重建的风险区间自第 6 分钟起：
        # 第一轮恰好抵达不受烟；在等的第二轮改道 south。
        late = {
            'corridor': 'west', 'device_clock_minute': 3, 'uploaded_minute': 5,
            'from_minute': 6, 'to_minute': 15,
        }
        controller.apply_receipt('G1', 6)
        order = controller.replan(late)
        self.assertEqual(controller.groups['G1']['state'], 'arrived')
        new_id = order['rerouted'][0]['new_group']
        controller.mark_departed(new_id)
        controller.apply_receipt(new_id, controller.groups[new_id]['arrive'])
        cert = controller.clearance_certificate()
        self.assertTrue(cert['issuable'], cert)
        self.assertEqual(cert['certified_clear_minute'],
                         controller.groups[new_id]['arrive'])
        self.assertTrue(cert['within_target'])


if __name__ == '__main__':
    unittest.main()
