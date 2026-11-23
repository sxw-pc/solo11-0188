"""Applicant identity, prerequisite certificate, and revocation."""
from collections import defaultdict

def calculate(events):
    rows=defaultdict(list)
    for e in events:
        if e['subsystem']=='candidate':rows[e['subject']].append(e)
    states={};entities=[];decisions=[]
    for person,items in rows.items():
        registrations=[e for e in items if e['action']=='register']
        certificates=[e for e in items if e['action']=='certificate']
        revocations=[e for e in items if e['action']=='revoke']
        registration=sorted(registrations,key=lambda e:(e['version'],e['at']))[-1] if registrations else None
        certificate=sorted(certificates,key=lambda e:(e['version'],e['at']))[-1] if certificates else None
        exam=registration['data'].get('exam_at','') if registration else ''
        dup=len({e['data'].get('booking_id') for e in registrations})>1
        valid=bool(certificate and certificate['data']['valid_from']<=exam<=certificate['data']['valid_to'])
        revoked=bool(certificate and any(e['data']['certificate_id']==certificate['data']['certificate_id'] and e['at']<=exam for e in revocations))
        eligible=bool(registration and valid and not dup and not revoked)
        states[person]={'eligible':eligible,'exam_at':exam,'booking_id':registration['data']['booking_id'] if registration else None,'credential_id':certificate['data']['certificate_id'] if certificate else None,'revoked':revoked}
        for e in items:
            kind='报名档案' if e['action']=='register' else '资格证明'
            entities.append(('考生资格核验',kind,e['id'],'revoked' if e['action']=='revoke' else 'recorded',e['id']))
        evidence=[e['id'] for e in items]
        for name,result,reason in [
            ('前置证书时效能否赋予考生资格','可考' if valid and not revoked else '无资格','证书有效期覆盖考试日且未追撤'),
            ('同身份重复报名能否占用两个名额','冲突冻结' if dup else '单名额','同一身份不得锁两个报名号'),
            ('证书追撤是否撤销未完成考试资格','撤销' if revoked else '保持','追撤按生效日改变未完成考试')]:
            decisions.append((person,name,result,reason,evidence))
    return states,entities,decisions
