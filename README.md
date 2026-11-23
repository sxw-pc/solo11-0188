# 职业技能考评实操工位采证台

完全离线的报名资格、工位能力、实操评判三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。所有原始文件按 SHA-256 保留，同事件异内容被拒绝，同内容重放幂等；导入、裁定和转移日志在单个事务中完成。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m skillstation demo --db demo.sqlite
python -m skillstation audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`candidate-import`、`station-import`、`assessment-import`，均使用 `--db` 和 `--file`。`fixtures/` 中每个入口都有 `-ok.json` 和 `-bad.json`，演示按正常再异常顺序逐一导入。`report` 输出当前裁定、历史、源文件摘要及每次旧值到新值的转移。三个跨账例子：证书追撤释放工位并使成绩待核；工位故障使成绩待核；实操事故反向冻结工位并触发补考。`audit` 从原证重建到内存库并比较报告。
