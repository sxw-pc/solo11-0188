"""Equipment calibration, tool editions, exclusive reservations, and incidents."""
from collections import defaultdict

def calculate(events,candidates):
    rows=defaultdict(list)
    for e in events:
        if e['subsystem']=='station':rows[e['subject']].append(e)
    incidents={e['data'].get('station_id') for e in events if e['subsystem']=='assessment' and e['action']=='incident' and e['data'].get('independent_witness')}
    states={};entities=[];decisions=[]
    for station,items in rows.items():
        machines=[e for e in items if e['action']=='machine']
        reserves=[e for e in items if e['action']=='reserve']
        faults=[e for e in items if e['action']=='fault']
        machine=sorted(machines,key=lambda e:(e['version'],e['at']))[-1] if machines else None
        window=machine['data']['window'] if machine else ''
        calibrated=bool(machine and machine['data']['calibrated_until']>=window and machine['data']['state']=='available')
        tools=bool(machine and set(machine['data']['required_tools']).issubset(set(machine['data']['present_tools'])))
        broken=bool(station in incidents or any(f['at']<=window for f in faults))
        eligible_res=[e for e in reserves if candidates.get(e['data']['candidate_id'],{}).get('eligible')]
        winner=sorted(eligible_res,key=lambda e:(e['at'],e['id']))[0] if eligible_res and calibrated and tools and not broken else None
        collision=len(eligible_res)>1
        states[station]={'available':calibrated and tools and not broken,'winner':winner['data']['candidate_id'] if winner else None,'collision':collision,'broken':broken,'window':window}
        for e in items:
            kind='工位档案' if e['action'] in ('machine','fault') else '时段预留'
            entities.append(('工位能力调度',kind,e['id'],e['action'],e['id']))
        evidence=[e['id'] for e in items]+[e['id'] for e in events if e['subsystem']=='candidate' and any(r['data']['candidate_id']==e['subject'] for r in reserves)]+[e['id'] for e in events if e['subsystem']=='assessment' and e['action']=='incident' and e['data'].get('station_id')==station]
        for name,result,reason in [
            ('工位校准是否覆盖考评窗口','可用' if calibrated else '封锁','设备校准覆盖完整考试窗口'),
            ('工具版本是否满足考试项目要求','齐备' if tools else '缺件','工具版本集合满足项目清单'),
            ('并发工位预留由谁取得时段',winner['data']['candidate_id'] if winner else '无确认','有资格考生按冻结时间排序独占工位'),
            ('设备中断是否产生补考权并形成可核验裁定','需补考' if broken else '无需','故障或独立事故见证使原工位不可继续')]:
            decisions.append((station,name,result,reason,evidence))
    return states,entities,decisions
