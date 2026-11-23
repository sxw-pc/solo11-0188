import json,tempfile,unittest
from pathlib import Path
from skillstation.core import connect,import_file,report,audit
from skillstation.policy import calculate

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def result(db,subject,name):
    return db.execute('SELECT result FROM current_decisions WHERE subject=? AND decision=?',(subject,name)).fetchone()[0]

class SkillWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def load(self,sub,phase='ok'):return import_file(self.db,sub,ROOT/f'{sub}-{phase}.json')
    def baseline(self):
        for sub in ('candidate','station','assessment'):self.load(sub)
    def test_ten_independent_decisions_and_replay(self):
        self.baseline();self.assertEqual(10,len(report(self.db)['decisions']))
        self.assertEqual('合格',result(self.db,'C1','关键步骤缺失是否判实操合格'))
        self.assertEqual('C1',result(self.db,'S1','并发工位预留由谁取得时段'))
        self.assertEqual(0,self.load('assessment')['inserted']);self.assertTrue(audit(self.db)['ok'])
    def test_revocation_releases_reservation_and_grade(self):
        self.baseline();self.load('candidate','bad')
        self.assertEqual('无确认',result(self.db,'S1','并发工位预留由谁取得时段'))
        self.assertEqual('不合格或待核',result(self.db,'C1','关键步骤缺失是否判实操合格'))
        self.assertEqual('撤销',result(self.db,'C1','证书追撤是否撤销未完成考试资格'))
    def test_incident_freezes_station_and_appeal(self):
        self.baseline();self.load('assessment','bad')
        self.assertEqual('需补考',result(self.db,'S1','设备中断是否产生补考权'))
        self.assertEqual('重开',result(self.db,'C1','迟到申诉是否重开成绩与工位责任'))
    def test_fault_propagates_to_score(self):
        self.baseline();self.load('station','bad')
        self.assertEqual('需补考',result(self.db,'S1','设备中断是否产生补考权'))
        self.assertEqual('不合格或待核',result(self.db,'C1','关键步骤缺失是否判实操合格'))
    def test_conflicting_event_is_atomic(self):
        self.baseline();before=report(self.db)
        event=json.loads((ROOT/'candidate-ok.json').read_text(encoding='utf-8'))[0];event['data']['booking_id']='tampered'
        p=Path(self.tmp.name)/'collision.json';p.write_text(json.dumps([event]),encoding='utf-8')
        with self.assertRaises(ValueError):import_file(self.db,'candidate',p)
        self.assertEqual(before,report(self.db))
    def test_independent_rule_boundaries(self):
        base=sum((json.loads((ROOT/f'{s}-ok.json').read_text(encoding='utf-8')) for s in ('candidate','station','assessment')),[])
        def decision(rows,subject,name):
            return next(d[2] for d in calculate(rows)[1] if d[0]==subject and d[1]==name)
        import copy
        rows=copy.deepcopy(base);rows[1]['data']['valid_to']='2026-01-01'
        self.assertEqual('无资格',decision(rows,'C1','前置证书时效能否赋予考生资格'))
        rows=copy.deepcopy(base);rows.append(dict(rows[0],id='c-register2',data=dict(booking_id='B2',exam_at='2026-12-10')))
        self.assertEqual('冲突冻结',decision(rows,'C1','同身份重复报名能否占用两个名额'))
        rows=copy.deepcopy(base);rows[2]['data']['calibrated_until']='2026-01-01'
        self.assertEqual('封锁',decision(rows,'S1','工位校准是否覆盖考评窗口'))
        rows=copy.deepcopy(base);rows[2]['data']['present_tools']=[]
        self.assertEqual('缺件',decision(rows,'S1','工具版本是否满足考试项目要求'))
        rows=copy.deepcopy(base);rows[5]['data']['assessor_id']='T1';rows[6]['data']['assessor_id']='T1'
        self.assertEqual('回避或缺评',decision(rows,'C1','考评员回避关系是否允许打分'))
        rows=copy.deepcopy(base);rows[4]['data']['completed_steps']=[]
        self.assertEqual('不合格或待核',decision(rows,'C1','关键步骤缺失是否判实操合格'))

if __name__=='__main__':unittest.main()
