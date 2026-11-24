# 职业技能考评实操工位采证台

完全离线的报名资格、工位能力、实操评判三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。所有原始文件按 SHA-256 保留，同事件异内容被拒绝，同内容重放幂等；导入、裁定和转移日志在单个事务中完成。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m skillstation demo --db demo.sqlite
python -m skillstation audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`candidate-import`、`station-import`、`assessment-import`，均使用 `--db` 和 `--file`。`fixtures/` 中每个入口都有 `-ok.json` 和 `-bad.json`，演示按正常再异常顺序逐一导入。`report` 输出当前裁定、历史、源文件摘要及每次旧值到新值的转移。三个跨账例子：证书追撤释放工位并使成绩待核；工位故障使成绩待核；实操事故反向冻结工位并触发补考。`audit` 从原证重建到内存库并比较报告。

## 过滤件联合预留账（批次 / 实物件号 / 级别 / 效期 / 考包 / 召回）

`skillstation/stock_capture.py` 给出批次级可用量（精确到实物 serial，含质检放行、隔离召回、级别替代与实际使用日效期）；`skillstation/kit_reservation.py` 的 `ReservationLedger` / `reserve()` 把批次验收、实物件数、级别替代、有效期和预装考包合成一本跨场排他账：

```python
from skillstation.kit_reservation import ReservationLedger
ledger = ReservationLedger.from_path("fixtures/kit-stock.json")  # 默认按 qc_documents 放行
plan = ledger.plan()
plan.flags()                 # {'exam-A': True, 'exam-B': True}
plan["exam-A"]["serials"]    # F1 的 4 个实物件号
ledger.batch_totals()        # {'F1': 4, 'F2': 4}，同批次跨场合计不超库存
ledger.procurement_summary() # 给采购员的采购/移库缺口（此路径为 []）

# 另一条质检原件路径：供应商召回证明 F1 配包前已隔离
quarantine = ReservationLedger(data, authorizations=data["recall_originals"])
quarantine.plan().flags()    # {'exam-A': False, 'exam-B': True}
quarantine.procurement_orders()  # exam-A 的 A 级缺口 4 件，B 级 F2 不得顶 A
```

已签收考包遇晚到召回：`ledger.recall_lot("F1", document_id=..., at=...)` 会追到具体 `K-*` 考包，写撤回单（`WDR-*`）与替换流转（`RPL-*`，有货则生成 `RPL-K-*` 替换包，无货则把 4 件缺口单挂到原考包与召回文号），原预留快照、签收人和签收时间等历史只追加、不改写；未受影响考包的件号清单保持不变。回归反例保证长有效期 A 批次不会被早到的 B 级贪心场抢走，未来 A-only 场优先。

```bash
python -B -m pytest -q -p no:cacheprovider
```
