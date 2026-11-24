"""Resource occupation and completion/remediation projection.

Captured consent, question-version coverage and on-site evidence drive who
actually occupied which resource and whether each service closed or needs
remedy. A booking table entry alone never closes a case.
"""
from .support_capture import capture, clock

def occupancy(data, plan_id):
    """Evidenced resource occupation: resource id -> time-ordered session windows."""
    busy = {}
    for session in data['plans'][plan_id].get('sessions', ()):
        busy.setdefault(session['resource'], []).append({
            'start': session['start'], 'end': session['end'],
            'candidate': session['candidate'], 'room': session['room']})
    for slots in busy.values():
        slots.sort(key=lambda s: (s['start'], s['end'], s['candidate']))
    return busy

def conflicts(data, plan_id):
    """Hard-constraint violations in evidenced occupation.

    One reader cannot serve two candidates in overlapping windows, and an
    offline device cannot be reused across rooms.
    """
    kinds = {r['id']: r.get('kind') for r in data['resources']}
    problems = []
    for resource, slots in occupancy(data, plan_id).items():
        for prev, cur in zip(slots, slots[1:]):
            if cur['start'] < prev['end']:
                problems.append({
                    'resource': resource, 'kind': 'overlap',
                    'candidates': sorted({prev['candidate'], cur['candidate']}),
                    'detail': '%s 重叠时段: %s %s-%s(%s) 与 %s %s-%s(%s)' % (
                        resource, prev['candidate'], clock(prev['start']), clock(prev['end']), prev['room'],
                        cur['candidate'], clock(cur['start']), clock(cur['end']), cur['room'])})
        rooms = sorted({s['room'] for s in slots})
        if kinds.get(resource) == 'device' and len(rooms) > 1:
            problems.append({
                'resource': resource, 'kind': 'cross-room',
                'candidates': sorted({s['candidate'] for s in slots}),
                'detail': '%s 跨室复用: %s' % (resource, ','.join(rooms))})
    return problems

_REMEDIES = {
    '计划未获双方签认': '补双方签认原计划后方可履约',
    '计划未覆盖已批准申请': '补排服务并通知考生',
    '无获批准的辅助申请': '补获批准的辅助申请',
    '资源未登记': '登记资源后重排',
    '超出资源时窗': '在资源时窗内重排服务',
    '资源方式不符': '核对资源辅助方式',
    '缺本人签署接受': '补本人签署接受原件，不得仅改方式字段',
    '缺可访问格式验收': '补可访问格式验收',
    '题版不一致': '改用与原卷完全相同的题目版本',
    '题版缺题': '补装与原卷完全相同的题目版本并重新验收',
    '题版多题': '移除异版题目并重新验收',
    '格式验收非本人': '由本人重新验收可访问格式',
    '无现场签到': '补真实签到，不得伪造他人在场',
    '签到人无权限': '由有权限考务人员重新签到',
    '迟到签到': '按实服时刻补记并安排缺口补时',
    '缺读题/会话记录': '补读题或会话记录',
    '缺离线会话回执': '补设备离线会话回执',
    '服务迟开始': '核实迟开始原因并安排补时',
    '服务未完成': '安排补足剩余服务',
    '无有效服务时段': '重排服务时段',
}

def _remedy(gap):
    return _REMEDIES.get(gap.split(':')[0], '补正后复核')

def manifest(data, plan_id, viewer):
    """Delivery ledger: per-candidate actual mode, start/end, gaps and remediation notices.

    ``closable`` is true only when every service is evidenced complete and no
    resource conflict exists, however the booking table reads.
    """
    records = capture(data, plan_id, viewer)
    problems = conflicts(data, plan_id)
    plan = data['plans'][plan_id]
    ledger = {}
    for candidate, rec in records.items():
        involved = [p for p in problems if candidate in p['candidates']]
        notices = ['补救通知: %s %s；%s' % (candidate, gap, _remedy(gap)) for gap in rec['gaps']]
        notices += ['补救通知: %s 涉冲突 %s；服务须重排核实' % (candidate, p['detail']) for p in involved]
        ledger[candidate] = {
            'requested_mode': rec['requested']['mode'] if rec['requested'] else None,
            'mode': rec['mode'], 'resource': rec['resource'],
            'start': rec['start'], 'end': rec['end'],
            'status': '完成' if not rec['gaps'] and not involved else '补救',
            'gaps': list(rec['gaps']), 'notices': notices,
            'evidence': [s['id'] for s in rec['sessions']],
        }
    return {
        'plan': plan_id, 'title': plan.get('title', plan_id),
        'occupancy': occupancy(data, plan_id), 'conflicts': problems,
        'closable': not problems and all(item['status'] == '完成' for item in ledger.values()),
        'ledger': ledger,
    }
