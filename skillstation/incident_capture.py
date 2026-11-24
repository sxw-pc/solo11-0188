"""S7 事故隔离副本：原始烟感时序、门洞通量、轮椅可达与放行/回执采录。

只采录走廊、通量窗口与分组级证据，不做逐人名册，不重算考生资格或成绩。
迟到烟感以设备原始时钟重建风险区间，上传时刻仅留痕，不参与区间计算。
"""


def routes_from(routes, station):
    """仅取指定工位的走廊列表（隔离副本，不触达考评账）。"""
    return [route for route in routes if route['station'] == station]


def route_by_id(routes, route_id):
    matches = [route for route in routes if route['id'] == route_id]
    if not matches:
        raise KeyError(f'未知走廊: {route_id}')
    return matches[0]


def risk_windows(smoke_events, corridor):
    """以设备原始时钟重建某走廊的受烟分钟区间，按起始分钟排序。

    迟到上传（uploaded_minute 晚于设备时钟）不推迟风险区间；
    设备时钟晚于上传时刻视为时钟异常并拒绝，防止伪造提前区间。
    """
    windows = []
    for event in smoke_events:
        if event['corridor'] != corridor:
            continue
        start, end = event['from_minute'], event['to_minute']
        if event['device_clock_minute'] > event['uploaded_minute']:
            raise ValueError('设备时钟晚于上传时刻，原始时钟不可信')
        if start > end:
            raise ValueError('烟感风险区间起始晚于结束')
        windows.append((start, end))
    return sorted(windows)


def is_smoked_at(smoke_events, corridor, minute):
    """走廊在该分钟是否处于受烟区间（半开区间 [from, to) 内不可分配）。"""
    return any(start <= minute < end
               for start, end in risk_windows(smoke_events, corridor))


def escort_capacity(escort_windows, route_id, minute):
    """该分钟该走廊可护送的轮椅人数；无护送资源窗口返回 0。"""
    total = 0
    for window in escort_windows:
        if window['route'] == route_id and window['from_minute'] <= minute < window['to_minute']:
            total += window['wheelchair_slots']
    return total


def window_violations(routes, smoke_events, escort_windows, groups):
    """复核已发分组流的硬约束，返回全部违规（容量、烟感、轮椅护送）。

    groups 为分组记录列表，每条含 route/depart/arrive/headcount/wheelchair。
    同一门洞占用窗重叠的分组人数之和不得超过核准容量；
    受烟分钟内不得有任何分组占用；轮椅分组出发分钟必须有护送资源且走廊可达。
    """
    violations = []
    for group in groups:
        route = route_by_id(routes, group['route'])
        depart, arrive = group['depart'], group['arrive']
        if depart >= arrive:
            violations.append({'group': group['id'], 'kind': '占用窗为空或倒置'})
        if any(is_smoked_at(smoke_events, route['id'], minute)
               for minute in range(depart, arrive)):
            violations.append({'group': group['id'], 'kind': '受烟分钟占用走廊',
                               'route': route['id']})
        if group.get('wheelchair', 0) > 0:
            if not route.get('accessible', False):
                violations.append({'group': group['id'], 'kind': '轮椅路线不可达',
                                   'route': route['id']})
            if escort_capacity(escort_windows, route['id'], depart) < group['wheelchair']:
                violations.append({'group': group['id'], 'kind': '出发分钟无轮椅护送资源',
                                   'route': route['id']})
    for route in routes:
        occupying = [g for g in groups if g['route'] == route['id']]
        for group in occupying:
            load = sum(other['headcount']
                       for other in occupying
                       if other['depart'] < group['arrive'] and other['arrive'] > group['depart'])
            if load > route['capacity']:
                violations.append({'group': group['id'], 'kind': '门洞通量超核准人数',
                                   'route': route['id'], 'load': load,
                                   'capacity': route['capacity']})
                break
    return violations


class IncidentFeed:
    """事故证据采集器：保留原始烟感、放行单与出口到达回执。

    所有原始记录原样留痕（含迟到上传的上传时刻），查询视图一律以
    设备原始时钟重建，保证事后可重放到同一结论。
    """

    def __init__(self, station, routes, smoke_events=None, escort_windows=None,
                 releases=None, receipts=None):
        self.station = station
        self.routes = routes_from(routes, station)
        self.smoke_events = list(smoke_events or [])
        self.escort_windows = list(escort_windows or [])
        self.releases = list(releases or [])
        self.receipts = list(receipts or [])
        self._validate()

    @classmethod
    def from_fixture(cls, data):
        return cls(data['station'], data['routes'], data.get('smoke_events'),
                   data.get('escort_windows'), data.get('releases'),
                   data.get('receipts'))

    def _validate(self):
        known = {route['id'] for route in self.routes}
        for event in self.smoke_events:
            if event['corridor'] not in known:
                raise ValueError(f"烟感指向未知走廊: {event['corridor']}")
            risk_windows(self.smoke_events, event['corridor'])
        for window in self.escort_windows:
            if window['route'] not in known:
                raise ValueError(f"护送窗口指向未知走廊: {window['route']}")
        for record in self.releases + self.receipts:
            if record.get('station', self.station) != self.station:
                raise ValueError('放行/回执不属于本网位')

    def add_smoke(self, event):
        """采录烟感（允许迟到上传），并立即用设备时钟重建风险区间。"""
        risk_windows([event], event['corridor'])
        self._validate_event_route(event['corridor'])
        self.smoke_events.append(event)

    def add_release(self, record):
        """采录现场放行：分组在某分钟被放行进入指定走廊。"""
        self._validate_event_route(record['route'])
        self.releases.append(dict(record, station=self.station))

    def add_receipt(self, record):
        """采录出口到达回执：只有回执才能证明分组实际清空。"""
        self._validate_event_route(record['route'])
        self.receipts.append(dict(record, station=self.station))

    def _validate_event_route(self, route_id):
        if route_id not in {route['id'] for route in self.routes}:
            raise ValueError(f'未知走廊: {route_id}')

    def is_open(self, route_id, minute):
        return not is_smoked_at(self.smoke_events, route_id, minute)

    def receipt_minutes(self):
        """分组 ID -> 出口到达回执分钟（无回执的分组不出现在映射中）。"""
        return {record['group_id']: record['minute'] for record in self.receipts}

    def released_groups(self):
        return {record['group_id']: record for record in self.releases}
