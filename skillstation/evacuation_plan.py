"""S7 时变分组疏散流与全场清空时间证书。

incident_capture 采录的原始烟感/通量窗口在此实际驱动逐分钟推演：
已报烟走廊在受影响分钟不再分配人员；门洞占用窗受核准通量约束；
轮椅分组只走可达且有护送资源的路线；无出口到达回执不得签发清空证书；
指挥单与改线以追加版本保存，并保留新旧人员分组映射。
不重算考生资格或成绩，不做逐人名册，主结果是路线容量与清场时刻。
"""
from .incident_capture import (
    IncidentFeed, routes_from, route_by_id, is_smoked_at,
    escort_capacity, window_violations,
)


def choose(routes, station, feed=None, minute=0):
    """当前分钟最快且可用的走廊 ID；全部受烟/不可用时抛错。

    旧行为“只比分钟数选 east”已废弃：east 已报烟时必须跳过。
    feed 缺省时仅按静态 smoke 标记判断，保持对简单夹具的可调用性。
    """
    candidates = []
    for route in routes_from(routes, station):
        if feed is not None:
            usable = feed.is_open(route['id'], minute)
        else:
            usable = not route.get('smoke', False)
        if usable:
            candidates.append(route)
    if not candidates:
        raise ValueError('当前分钟无可用走廊')
    return min(candidates, key=lambda route: route['minutes'])['id']


class EvacuationController:
    """从事故证据重建的分组疏散指挥器，支持改线重算与版本化指挥单。"""

    def __init__(self, feed, people, wheelchair=0, clear_by_minute=15):
        self.feed = feed
        self.people = people
        self.wheelchair = wheelchair
        self.clear_by = clear_by_minute
        self.orders = []
        self.groups = {}
        self.lineage = []

    @classmethod
    def from_fixture(cls, data):
        feed = IncidentFeed.from_fixture(data)
        controller = cls(feed, data['people'], data.get('wheelchair', 0),
                         data['clear_by_minute'])
        for event in feed.smoke_events:
            controller._apply_smoke(event, replay=True)
        return controller

    # -- 方案枚举 -------------------------------------------------------

    def feasible_plans(self):
        """枚举每条可独立完成清场的走廊方案，按最迟到达分钟升序。

        每条方案给出分组批次、每组出发/到达时刻、门洞占用与第一组到达。
        受烟走廊（在清场窗口内有风险区间）直接排除，不做“删 east 再选最短”。
        """
        plans = []
        for route in self.feed.routes:
            if any(is_smoked_at(self.feed.smoke_events, route['id'], minute)
                   for minute in range(self.clear_by + 1)):
                continue
            plan = self._plan_on_route(route)
            if plan is not None:
                plans.append(plan)
        plans.sort(key=lambda plan: (plan['clear_minute'], plan['route']))
        return plans

    def _plan_on_route(self, route):
        remaining = self.people
        wheelchair_left = self.wheelchair
        batches = []
        depart = 0
        while remaining > 0:
            headcount = min(route['capacity'], remaining)
            wheelchair = min(wheelchair_left, headcount) if wheelchair_left else 0
            depart = self._earliest_departure(route, headcount, wheelchair, depart, batches)
            if depart is None or depart >= self.clear_by:
                return None
            arrive = depart + route['minutes']
            if arrive > self.clear_by:
                return None
            batches.append({
                'route': route['id'], 'depart': depart, 'arrive': arrive,
                'headcount': headcount, 'wheelchair': wheelchair,
            })
            remaining -= headcount
            wheelchair_left -= wheelchair
            depart += 1
        return {
            'route': route['id'],
            'batches': batches,
            'first_arrival': batches[0]['arrive'],
            'clear_minute': batches[-1]['arrive'],
            'within_target': batches[-1]['arrive'] <= self.clear_by,
        }

    def _earliest_departure(self, route, headcount, wheelchair, start, batches):
        """逐分钟寻找满足开放、通量重叠与轮椅护送的最早出发分钟。"""
        minute = start
        while minute <= self.clear_by:
            if is_smoked_at(self.feed.smoke_events, route['id'], minute):
                minute += 1
                continue
            if wheelchair and (not route.get('accessible', False) or
                               escort_capacity(self.feed.escort_windows, route['id'], minute) < wheelchair):
                minute += 1
                continue
            window_end = minute + route['minutes']
            overlap = sum(b['headcount'] for b in batches
                          if b['depart'] < window_end and b['arrive'] > minute)
            if overlap + headcount <= route['capacity']:
                return minute
            minute += 1
        return None

    # -- 指挥单与分组 ---------------------------------------------------

    def issue_order(self, route_id, issued_minute=0):
        """按指定走廊签发指挥单并生成分组；该走廊必须能独立完成清场。"""
        if self.orders:
            raise ValueError('指挥单已存在，改线请用 replan')
        plan = next((candidate for candidate in self.feasible_plans()
                     if candidate['route'] == route_id), None)
        if plan is None:
            raise ValueError(f'走廊 {route_id} 无法在 {self.clear_by} 分钟内完成清场')
        order = {
            'version': 1, 'route': route_id, 'issued_minute': issued_minute,
            'plan': plan, 'groups': [],
        }
        for index, batch in enumerate(plan['batches'], start=1):
            group_id = f'G{index}'
            group = {
                'id': group_id, 'route': route_id,
                'depart': batch['depart'], 'arrive': batch['arrive'],
                'headcount': batch['headcount'],
                'wheelchair': batch['wheelchair'],
                'state': 'waiting', 'order_version': 1,
                'smoke_exposed': False, 'receipt_minute': None,
            }
            self.groups[group_id] = group
            order['groups'].append(self._snapshot(group))
            self.feed.add_release({'group_id': group_id, 'route': route_id,
                                   'minute': batch['depart'], 'order_version': 1})
        self.orders.append(order)
        return order

    def active_groups(self):
        """未被改线取代的分组（已抵达组永久保留，不会倒退回考场）。"""
        return [group for group in self.groups.values()
                if group['state'] != 'superseded']

    def _snapshot(self, group):
        return {key: group[key] for key in
                ('id', 'route', 'depart', 'arrive', 'headcount', 'wheelchair',
                 'state', 'order_version', 'smoke_exposed', 'receipt_minute')}

    # -- 迟到烟感与改线 -------------------------------------------------

    def replan(self, smoke_event, issued_minute=None):
        """采录新烟感并对受影响的在等分组改道，在途/已抵达组保持原线。

        风险区间以设备原始时钟重建；在途分组若其占用窗落入受烟分钟，
        标记 smoke_exposed 但不移动、不倒退，等待出口回执定案。
        返回新版本指挥单（追加保存），含新旧分组映射。
        """
        event = self._apply_smoke(smoke_event)
        if issued_minute is None:
            issued_minute = event['uploaded_minute']
        if not self.orders:
            raise ValueError('尚无指挥单，无需改线')
        corridor = event['corridor']
        start, end = event['from_minute'], event['to_minute']
        rerouted, mapping = [], []
        for group in self.active_groups():
            if group['route'] != corridor:
                continue
            affected = start < group['arrive'] and end > group['depart']
            if not affected:
                continue
            if group['state'] in ('in_transit', 'arrived'):
                group['smoke_exposed'] = True
                continue
            new_group = self._reroute(group, issued_minute)
            rerouted.append(new_group)
            mapping.append({'old_group': group['id'], 'new_group': new_group['id'],
                            'old_route': group['route'], 'new_route': new_group['route'],
                            'headcount': new_group['headcount'],
                            'wheelchair': new_group['wheelchair']})
        version = len(self.orders) + 1
        order = {
            'version': version, 'route': None,
            'issued_minute': issued_minute, 'trigger_smoke': event,
            'rerouted': mapping,
            'groups': [self._snapshot(group) for group in self.active_groups()],
        }
        self.orders.append(order)
        self.lineage.append({'version': version, 'mapping': mapping})
        return order

    def _apply_smoke(self, event, replay=False):
        if not replay:
            self.feed.add_smoke(event)
        return event

    def _reroute(self, group, not_before):
        """用当前在途位置与剩余通量逐分钟重算可改道出口。"""
        alternatives = [route for route in self.feed.routes
                        if route['id'] != group['route']]
        alternatives.sort(key=lambda route: route['minutes'])
        for route in alternatives:
            batches = [other for other in self.active_groups()
                       if other['route'] == route['id'] and other is not group]
            depart = self._earliest_departure(
                route, group['headcount'], group['wheelchair'],
                max(not_before, group['depart']), batches)
            if depart is None:
                continue
            arrive = depart + route['minutes']
            if arrive > self.clear_by:
                continue
            new_id = f"G{len(self.groups) + 1}"
            group['state'] = 'superseded'
            new_group = {
                'id': new_id, 'route': route['id'], 'depart': depart,
                'arrive': arrive, 'headcount': group['headcount'],
                'wheelchair': group['wheelchair'], 'state': 'waiting',
                'order_version': len(self.orders) + 1,
                'smoke_exposed': False, 'receipt_minute': None,
                'rerouted_from': group['id'],
            }
            self.groups[new_id] = new_group
            self.feed.add_release({'group_id': new_id, 'route': route['id'],
                                   'minute': depart,
                                   'order_version': len(self.orders) + 1})
            return new_group
        raise ValueError(f"分组 {group['id']} 无可改道出口")

    # -- 现场放行与到达回执 ---------------------------------------------

    def mark_departed(self, group_id, minute=None):
        group = self._require_group(group_id)
        if group['state'] != 'waiting':
            raise ValueError(f"分组 {group_id} 不在待放行状态")
        group['state'] = 'in_transit'
        return group

    def apply_receipt(self, group_id, minute):
        """登记出口到达回执；未收到回执的分组永不视为已清空。"""
        group = self._require_group(group_id)
        if group['state'] == 'superseded':
            raise ValueError('被取代分组不再接受回执')
        if minute < group['depart']:
            raise ValueError('到达回执早于出发分钟')
        group['state'] = 'arrived'
        group['receipt_minute'] = minute
        self.feed.add_receipt({'group_id': group_id, 'route': group['route'],
                               'minute': minute})
        return group

    def _require_group(self, group_id):
        if group_id not in self.groups:
            raise KeyError(f'未知分组: {group_id}')
        return self.groups[group_id]

    # -- 证书 -----------------------------------------------------------

    def clearance_certificate(self):
        """基于回执签发全场清空时间证书；无全员回执则不可签发。"""
        active = self.active_groups()
        receipted = [group for group in active if group['receipt_minute'] is not None]
        missing = [group['id'] for group in active
                   if group['receipt_minute'] is None]
        exposed = [group['id'] for group in active if group['smoke_exposed']]
        violations = window_violations(
            self.feed.routes, self.feed.smoke_events,
            self.feed.escort_windows, active)
        issuable = not missing and not violations and not exposed
        planned_clear = max((group['arrive'] for group in active), default=None)
        certified_clear = max((group['receipt_minute'] for group in receipted),
                              default=None) if issuable else None
        return {
            'station': self.feed.station,
            'people': self.people,
            'group_count': len(active),
            'planned_clear_minute': planned_clear,
            'certified_clear_minute': certified_clear,
            'issuable': issuable,
            'within_target': issuable and certified_clear <= self.clear_by,
            'missing_receipts': missing,
            'smoke_exposed_groups': exposed,
            'violations': violations,
            'order_versions': [order['version'] for order in self.orders],
        }
