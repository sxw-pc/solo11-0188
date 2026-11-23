"""Independent assessor votes, mandatory safety steps, appeal and equipment fault."""
from collections import defaultdict

def calculate(events,candidates,stations):
    rows=defaultdict(list)
    for e in events:
        if e['subsystem']=='assessment':rows[e['subject']].append(e)
    states={};entities=[];decisions=[]
    for person,items in rows.items():
        attempts=[e for e in items if e['action']=='attempt']
        reviews=[e for e in items if e['action']=='review']
        appeals=[e for e in items if e['action']=='appeal']
        attempt=sorted(attempts,key=lambda e:(e['version'],e['at']))[-1] if attempts else None
        station_id=attempt['data']['station_id'] if attempt else None
        station=stations.get(station_id,{})
        valid_reviews=[e for e in reviews if e['data'].get('assessor_id') not in (person,attempt['data'].get('trainer_id') if attempt else None)]
        independent=len({e['data']['assessor_id'] for e in valid_reviews})>=2
        safe=bool(attempt and set(attempt['data']['required_steps']).issubset(set(attempt['data']['completed_steps'])))
        score=min((e['data']['score'] for e in valid_reviews),default=0)
        ready=bool(candidates.get(person,{}).get('eligible') and station.get('winner')==person and station.get('available'))
        appeal=bool(appeals and max(e['version'] for e in appeals)>= (attempt['version'] if attempt else 0))
        passed=ready and independent and safe and score>=60 and not appeal
        states[person]={'passed':passed,'score':score,'appeal':appeal,'station_id':station_id,'ready':ready}
        for e in items:
            kind='操作记录' if e['action'] in ('attempt','incident') else '成绩裁定'
            entities.append(('实操评判与复核',kind,e['id'],e['action'],e['id']))
        evidence=[e['id'] for e in items]+[e['id'] for e in events if e['subsystem']=='candidate' and e['subject']==person]+[e['id'] for e in events if e['subsystem']=='station' and e['subject']==station_id]
        for name,result,reason in [
            ('考评员回避关系是否允许打分','有效双评' if independent else '回避或缺评','评分人须互异且独立于授课方'),
            ('关键步骤缺失是否判实操合格','合格' if passed else '不合格或待核','安全步骤、资格、工位与双评同时满足'),
            ('迟到申诉是否重开成绩与工位责任','重开' if appeal else '维持','申诉保留原成绩并重开责任')]:
            decisions.append((person,name,result,reason,evidence))
    return states,entities,decisions
