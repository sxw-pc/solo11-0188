"""Capture approved requests, signed consent, question-version coverage and on-site evidence.

Personal assistance needs are restricted to authorised exam staff. Every
projection keeps the original approved commitment next to the on-site
evidence, so notified or used services stay auditable and a late check-in
only ever backfills service that was actually delivered.
"""

def clock(hour):
    """Render a fractional hour as HH:MM."""
    whole = int(hour)
    return '%02d:%02d' % (whole, round((hour - whole) * 60))

def _require_staff(data, viewer):
    if viewer not in data.get('staff', ()):
        raise PermissionError('个人辅助需求仅向有权限的考务人员展示: %r' % (viewer,))

def requests_for(data, candidate, viewer):
    """Approved assistance requests for one candidate; staff-only."""
    _require_staff(data, viewer)
    return [item for item in data['requests'] if item['candidate'] == candidate]

def checkin_for(data, candidate):
    """The candidate's own on-site check-in; another person's presence never counts."""
    return next((c for c in data.get('checkins', ()) if c['candidate'] == candidate), None)

def consent_for(plan, candidate, mode):
    """Signed acceptance by the candidate themselves for switching to mode."""
    return next((c for c in plan.get('consents', ())
                 if c['candidate'] == candidate and c['mode'] == mode and c.get('signed')), None)

def format_for(plan, resource):
    """Accessible-format acceptance record for a device resource."""
    return next((f for f in plan.get('formats', ()) if f['resource'] == resource), None)

def sessions_for(plan, candidate=None, resource=None):
    """Reading logs / offline session receipts, optionally filtered."""
    return [s for s in plan.get('sessions', ())
            if (candidate is None or s['candidate'] == candidate)
            and (resource is None or s['resource'] == resource)]

def capture(data, plan_id, viewer):
    """Evidence-backed service record per candidate for one signed plan.

    Signed consent, exact question-version coverage and on-site evidence
    decide what was actually delivered; ``gaps`` lists every unmet hard
    requirement. A mode field alone never delivers a service.
    """
    _require_staff(data, viewer)
    plan = data['plans'][plan_id]
    paper = data['paper']
    resources = {r['id']: r for r in data['resources']}
    signed = all(plan.get('signatures', {}).get(s['candidate']) for s in plan['services'])
    records = {}
    for service in plan['services']:
        candidate = service['candidate']
        request = next((r for r in data['requests'] if r['candidate'] == candidate), None)
        resource = resources.get(service['resource'])
        gaps = []
        if not signed:
            gaps.append('计划未获双方签认')
        if request is None:
            gaps.append('无获批准的辅助申请')
        if resource is None:
            gaps.append('资源未登记: %s' % service['resource'])
        else:
            if not resource['window'][0] <= service['start'] < service['end'] <= resource['window'][1]:
                gaps.append('超出资源时窗')
            if resource['mode'] != service['mode']:
                gaps.append('资源方式不符')
        consent = None
        if request and service['mode'] != request['mode']:
            consent = consent_for(plan, candidate, service['mode'])
            if consent is None:
                gaps.append('缺本人签署接受: %s' % service['mode'])
        fmt = None
        if resource and resource.get('kind') == 'device':
            fmt = format_for(plan, service['resource'])
            if fmt is None:
                gaps.append('缺可访问格式验收')
            else:
                if fmt.get('paper') != paper['id']:
                    gaps.append('题版不一致')
                missing = [q for q in paper['items'] if q not in fmt.get('items', ())]
                extra = [q for q in fmt.get('items', ()) if q not in paper['items']]
                if missing:
                    gaps.append('题版缺题: %s' % ','.join(missing))
                if extra:
                    gaps.append('题版多题: %s' % ','.join(extra))
                if fmt.get('accepted_by') != candidate:
                    gaps.append('格式验收非本人')
        checkin = checkin_for(data, candidate)
        if checkin is None:
            gaps.append('无现场签到')
        elif checkin.get('by') not in data.get('staff', ()):
            gaps.append('签到人无权限')
        sessions = [s for s in sessions_for(plan, candidate, service['resource'])
                    if s['room'] == service['room']]
        start = end = None
        if not sessions:
            gaps.append('缺读题/会话记录')
        else:
            session = sorted(sessions, key=lambda s: (s['start'], s['id']))[0]
            if session.get('paper') != paper['id']:
                gaps.append('题版不一致')
            if resource and resource.get('kind') == 'device' and not session.get('offline'):
                gaps.append('缺离线会话回执')
            start, end = session['start'], session['end']
            if checkin and checkin['at'] > start:
                gaps.append('迟到签到: %s 到岗' % clock(checkin['at']))
                start = checkin['at']  # late arrival: only real service after check-in counts
            elif start > service['start']:
                gaps.append('服务迟开始: %s' % clock(start))
            if end < service['end']:
                gaps.append('服务未完成')
            if start >= end:
                gaps.append('无有效服务时段')
        records[candidate] = {
            'candidate': candidate, 'requested': request, 'mode': service['mode'],
            'resource': service['resource'], 'room': service['room'],
            'planned_start': service['start'], 'planned_end': service['end'],
            'start': start, 'end': end, 'consent': consent, 'format': fmt,
            'checkin': checkin, 'sessions': sessions, 'gaps': gaps,
        }
    for request in data['requests']:
        if request['candidate'] not in records:
            records[request['candidate']] = {
                'candidate': request['candidate'], 'requested': request, 'mode': None,
                'resource': None, 'room': None, 'planned_start': None, 'planned_end': None,
                'start': None, 'end': None, 'consent': None, 'format': None,
                'checkin': checkin_for(data, request['candidate']), 'sessions': [],
                'gaps': ['计划未覆盖已批准申请'],
            }
    return records
