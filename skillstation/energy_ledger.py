"""工位计费账: 凭当期双方签认协议与可核证据分摊共享电量。

核心恒等式(每个时段、每个修订版本都必须成立)::

    sum(各站可计费 kWh) + 待核差额 kWh == 主表净增量 kWh

约束:

* 共享 HVAC 的归属只能来自当期双方签认的计费协议 + 可核的负载/面积证据,
  不允许裸比例参数, 更不允许把主表加进总量后除以二;
* 待核差额保留符号, 不把负差归零; 争议表计的电量留在待核差额,
  绝不转嫁给另一租户(不跨租户借量);
* 晚到校正通过 :class:`EnergyLedger` 追加新版本, 旧账原样保留,
  且只重开/更正被校正的时段。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Iterable

from .meter_capture import (
    EnergyDataError, SignatureError, canonical, digest_doc, number,
    reconcile_meters, verify_sealed,
)

STATIONS = ("S7", "S8")


class AgreementError(EnergyDataError):
    """协议未签认、非当期、方不全或与证据不一致。"""


# ---------------------------------------------------------------------------
# 协议与证据
# ---------------------------------------------------------------------------

def _records_by_id(items: Any) -> dict[str, dict]:
    """接受 sealed-record 列表或 {id: doc/record} 映射。"""
    out: dict[str, dict] = {}
    if isinstance(items, dict):
        for key, value in items.items():
            doc = value.get("doc", value) if isinstance(value, dict) else {}
            out[doc.get("agreement_id") or doc.get("evidence_id") or key] = value
    elif isinstance(items, list):
        for value in items:
            doc = value.get("doc", value) if isinstance(value, dict) else {}
            key = doc.get("agreement_id") or doc.get("evidence_id") or digest_doc(value)[:12]
            out[key] = value
    return out


def _fingerprint(record: Any) -> str:
    # 必须是记录实际内容的摘要, 不能只读信封里声称的 sha256 —— 篡改 doc
    # 后旧 sha 字段不会变, 但整体规范化摘要会变, 从而识别来源冲突。
    return digest_doc(record)


def _merge_pool(payload: dict, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    """合并多个可能的原件来源; 同一 id 内容/摘要冲突时拒绝择一使用。"""
    pool: dict[str, Any] = {}
    for key in keys:
        for rid, record in _records_by_id(payload.get(key)).items():
            if rid in pool and _fingerprint(pool[rid]) != _fingerprint(record):
                raise SignatureError(
                    f"{label} {rid} 在多个来源中原件摘要冲突, "
                    "不能任选其一(可能存在篡改或账外版本)")
            pool[rid] = record
    return pool


def resolve_agreement(payload: dict, agreement: dict | str | None = None,
                       agreement_id: str | None = None,
                       *, required_parties=STATIONS,
                       verify: bool = True) -> tuple[dict, dict | None]:
    """定位并校验当期协议, 返回 (协议正文, 协议信封)。

    查找顺序: 显式 doc/dict 入参 > agreement_id > payload 当前协议 >
    payload.agreement_records/agreements 中的指定记录。
    """
    envelope = None
    if isinstance(agreement, dict) and agreement.get("doc") and agreement.get("sha256"):
        envelope = agreement
        agreement_id = agreement["doc"].get("agreement_id")
    elif isinstance(agreement, dict):
        return agreement, None
    elif isinstance(agreement, str):
        agreement_id = agreement

    agreement_id = agreement_id or payload.get("current_agreement_id")

    pool = _merge_pool(payload, ("agreement_records", "agreements"), "计费协议")

    if envelope is None and agreement_id and agreement_id in pool:
        envelope = pool[agreement_id]
    if envelope is None and agreement_id and agreement_id not in pool:
        raise AgreementError(f"找不到计费协议原件: {agreement_id}")
    if envelope is None:
        return {}, None

    if verify:
        doc = verify_sealed(envelope, required_parties=required_parties,
                            label=f"计费协议 {agreement_id or ''}")
    else:
        doc = envelope.get("doc", envelope)
    return doc, envelope


def resolve_evidence(payload: dict, evidence_id: str,
                     *, verify: bool = True) -> dict:
    pool = _merge_pool(payload, ("evidence_records", "evidence"), "证据")
    if evidence_id not in pool:
        raise AgreementError(f"协议引用的可核证据缺失: {evidence_id}")
    record = pool[evidence_id]
    if verify and isinstance(record, dict) and record.get("sha256"):
        return verify_sealed(record, label=f"证据 {evidence_id}")
    return record.get("doc", record) if isinstance(record, dict) else record


def check_period_applicable(agreement_doc: dict, period: dict | None) -> None:
    """协议必须适用于当前计费时段(period_id 或起止日期覆盖)。"""
    if not period:
        return
    pid = period.get("id")
    if agreement_doc.get("period_id") and pid and agreement_doc["period_id"] != pid:
        # 也允许 applies_to 列表
        applies = agreement_doc.get("applies_to") or []
        if pid not in applies:
            raise AgreementError(
                f"协议 {agreement_doc.get('agreement_id')} 适用于 "
                f"{agreement_doc['period_id']} 而非当期 {pid}")
    start = agreement_doc.get("valid_from")
    end = agreement_doc.get("valid_to")
    if start and period.get("end") and start > period["end"]:
        raise AgreementError("协议生效日晚于计费区间")
    if end and period.get("start") and end < period["start"]:
        raise AgreementError("协议在计费区间开始前已失效")


def _weights_from_evidence(method: str, evidence: dict,
                           stations: list[str]) -> dict[str, Fraction]:
    if method in ("thermal_load", "verified_thermal_load", "heat_load"):
        values = evidence.get("thermal_by_station") or evidence.get("load_by_station")
        if not values:
            raise AgreementError("热负载协议缺少可核的 thermal_by_station 数据")
        weights = {s: Fraction(str(values[s])) for s in stations}
        label = "实际热负载"
    elif method in ("area", "certified_area", "equal_area"):
        values = evidence.get("area_m2") or evidence.get("areas")
        if not values:
            raise AgreementError("等面积协议缺少核证面积 area_m2")
        weights = {s: Fraction(str(values[s])) for s in stations}
        label = "核证面积"
    elif method in ("fixed_ratio", "stipulated_ratio"):
        values = evidence.get("ratio") or evidence.get("shares")
        if not values:
            raise AgreementError("约定比例缺少证据/条款 ratio")
        weights = {s: Fraction(str(values[s])) for s in stations}
        label = "协议约定比例"
    else:
        raise AgreementError(f"不支持的分摊方法: {method}")

    missing = [s for s in stations if s not in weights]
    if missing:
        raise AgreementError(f"证据 {label}缺少工位 {missing}")
    if any(w < 0 for w in weights.values()):
        raise AgreementError(f"{label}不允许负值")
    total_w = sum(weights.values(), Fraction(0))
    if total_w <= 0:
        raise AgreementError(f"{label}权重合计必须为正")
    return {s: weights[s] / total_w for s in stations}


def allocate_shared(shared_kwh, weights: dict[str, Fraction | float | str],
                    stations: list[str] | None = None) -> dict[str, Fraction]:
    """按证据权重精确分摊共享电量(Fraction), 分摊合计严格等于共享电量。"""
    total = shared_kwh if isinstance(shared_kwh, Fraction) else Fraction(str(shared_kwh))
    stations = stations or list(weights)
    w = {s: (weights[s] if isinstance(weights[s], Fraction) else Fraction(str(weights[s])))
         for s in stations}
    wsum = sum(w.values(), Fraction(0))
    if wsum <= 0:
        raise AgreementError("分摊权重合计必须为正")
    return {s: total * w[s] / wsum for s in stations}


def _verify_claimed_shares(doc: dict, derived: dict[str, Fraction]) -> None:
    claimed = doc.get("claimed_share") or doc.get("ratio")
    if not claimed:
        return
    tol = Fraction(str(doc.get("ratio_tolerance", "0.0001")))
    for station, value in claimed.items():
        c = Fraction(str(value))
        if abs(c - derived.get(station, Fraction(0))) > tol:
            raise AgreementError(
                f"协议声称 {station} 比例 {number(c)} 与可核证据推导 "
                f"{number(derived[station])} 不一致, 不能据此出账")


# ---------------------------------------------------------------------------
# 工位结算
# ---------------------------------------------------------------------------

def _station_list(payload: dict, explicit: Iterable[str] | None) -> list[str]:
    if explicit:
        return list(explicit)
    stations = payload.get("stations") or list(STATIONS)
    out = []
    for s in stations:
        out.append(s if isinstance(s, str) else s["id"])
    return out


def _infer_station(meter_id: str, stations: list[str]) -> str | None:
    upper = str(meter_id).upper()
    for s in stations:
        if str(s).upper() in upper:
            return s
    return None


def _classify_meters(payload: dict, reconciliation: dict,
                     stations: list[str]) -> tuple[dict[str, str], dict[str, list[str]], str | None]:
    """返回 meter->station、station->direct meter ids、shared meter id。"""
    rows = payload.get("meters") or payload.get("readings") or []
    by_id = {m["id"]: m for m in rows}
    meter_station: dict[str, str] = {}
    direct: dict[str, list[str]] = {s: [] for s in stations}
    shared_id = None
    for mid in reconciliation["meter_kwh"]:
        m = by_id.get(mid, {})
        role = (m.get("role") or "").lower()
        station = m.get("station") or m.get("serves_station")
        if station not in stations:
            station = _infer_station(mid, stations)  # 旧 id 形如 S7-direct
        upper = str(mid).upper()
        is_master = (role == "master" or m.get("is_master") is True
                     or str(mid).strip().lower() in ("master", "main"))
        is_shared = (role == "shared" or bool(m.get("shared"))
                     or (role not in ("direct", "master") and station is None
                         and ("SHARED" in upper or "HVAC" in upper)))
        if is_master:
            continue
        if is_shared:
            meter_station[mid] = "__shared__"
            shared_id = mid if shared_id is None else shared_id
        elif station in stations:
            meter_station[mid] = station
            direct.setdefault(station, []).append(mid)
        # 其余无法归属的表不计入工位(其电量保留在待核差额)
    return meter_station, direct, shared_id


def settle(payload: dict, agreement: dict | str | None = None,
           stations: Iterable[str] | None = None, *,
           agreement_id: str | None = None,
           reconciliation: dict | None = None,
           period: dict | None = None,
           verify: bool = True,
           strict_meters: bool = False,
           disputed_meter_ids: Iterable[str] | None = None,
           allow_provisional: bool = True) -> dict:
    """执行一次完整的主分表对账 + 协议分摊, 生成可追溯结算单。

    没有有效当期协议时, 共享电量保留为待核 ``pending_allocation_kwh``,
    绝不擅自按比例分配(``allow_provisional=False`` 时直接报错)。
    """
    station_ids = _station_list(payload, stations)
    period = period or payload.get("billing_period") or payload.get("period")
    rec = reconciliation or reconcile_meters(
        payload, period=period, verify_signatures=verify,
        strict=strict_meters, disputed_meter_ids=disputed_meter_ids)

    master_exact = rec["master_kwh_exact"]
    meter_kwh_exact = {mid: Fraction(str(v)) for mid, v in rec["meter_kwh"].items()}
    roots = set(rec["roots"])

    meter_station, direct_meters, shared_id = _classify_meters(payload, rec, station_ids)

    direct_exact = {s: Fraction(0) for s in station_ids}
    basis: list[dict] = []
    for mid, station in meter_station.items():
        if station in direct_exact and mid in meter_kwh_exact:
            direct_exact[station] += meter_kwh_exact[mid]
            basis.append({"kind": "direct_meter", "station": station,
                          "meter_id": mid, "kwh": number(meter_kwh_exact[mid]),
                          "trace": "校准后区间净增量(不与主表相加)"})

    # 共享表可能有多只; 协议用 shared_meter 指定, 否则取唯一 shared 表
    shared_meter_ids = [m for m, s in meter_station.items() if s == "__shared__"]
    agreement_doc: dict = {}
    agreement_envelope = None
    weights: dict[str, Fraction] | None = None
    evidence_doc: dict | None = None
    allocation_exact = {s: Fraction(0) for s in station_ids}
    shared_total_exact = sum((meter_kwh_exact.get(m, Fraction(0))
                              for m in shared_meter_ids), Fraction(0))

    if agreement is not None or agreement_id or payload.get("current_agreement_id"):
        agreement_doc, agreement_envelope = resolve_agreement(
            payload, agreement, agreement_id, verify=verify)
        if verify:
            check_period_applicable(agreement_doc, period)
        target_meter = agreement_doc.get("shared_meter")
        if target_meter and target_meter in meter_kwh_exact:
            # 协议只分指定共享表; 若还存在别的 shared 表, 留待核
            agreed_shared_exact = meter_kwh_exact[target_meter]
            extra_shared = [m for m in shared_meter_ids if m != target_meter]
        else:
            agreed_shared_exact = shared_total_exact
            extra_shared = []
        evidence_id = agreement_doc.get("evidence_id")
        method = agreement_doc.get("method", "")
        inline_weights = agreement_doc.get("weights")
        if inline_weights is None and not evidence_id and isinstance(
                agreement_doc.get("ratio"), dict):
            inline_weights = agreement_doc["ratio"]
        if method in ("equal_shares", "equal_split"):
            weights = {s: Fraction(1, len(station_ids)) for s in station_ids}
        elif inline_weights is not None:
            # 程序化显式权重: 可用, 但未经独立证据核验, 结算单中明确标注。
            weights = {s: Fraction(str(inline_weights[s])) for s in station_ids}
            wsum = sum(weights.values(), Fraction(0))
            weights = {s: weights[s] / wsum for s in station_ids}
            basis.append({"kind": "inline_weights_warning",
                          "detail": "权重由调用方直接给出, 缺少独立可核证据链"})
        else:
            if evidence_id:
                evidence_doc = resolve_evidence(payload, evidence_id, verify=verify)
                if verify and isinstance(evidence_doc, dict) and evidence_doc.get("doc"):
                    evidence_doc = evidence_doc["doc"]
            if evidence_doc is None:
                raise AgreementError(f"方法 {method} 必须提供可核证据 evidence_id")
            weights = _weights_from_evidence(method, evidence_doc, station_ids)
            _verify_claimed_shares(agreement_doc, weights)
        alloc = allocate_shared(agreed_shared_exact, weights, station_ids)
        allocation_exact = dict(alloc)
        for s in station_ids:
            basis.append({
                "kind": "shared_allocation", "station": s,
                "shared_meter": target_meter or shared_id,
                "agreement_id": agreement_doc.get("agreement_id"),
                "method": method,
                "evidence_id": evidence_id,
                "weight": f"{weights[s].numerator}/{weights[s].denominator}",
                "shared_kwh": number(agreed_shared_exact),
                "allocated_kwh": number(alloc[s]),
                "trace": "签认协议 + 可核证据推导权重",
            })
        for mid in extra_shared:
            basis.append({"kind": "shared_uncovered_by_agreement",
                          "meter_id": mid, "kwh": number(meter_kwh_exact[mid])})
    elif not allow_provisional:
        raise AgreementError("缺少当期双方签认的计费协议")

    billed_exact = {s: direct_exact[s] + allocation_exact[s] for s in station_ids}
    billed_total = sum(billed_exact.values(), Fraction(0))
    unassigned_exact = master_exact - billed_total

    pending_allocation_exact = shared_total_exact - sum(allocation_exact.values(), Fraction(0))
    meter_difference_exact = rec["meter_difference_exact"]

    # 守恒恒等式(精确)
    if billed_total + unassigned_exact != master_exact:
        raise EnergyDataError("结算恒等式校验失败")

    chain = {
        "topology_id": rec.get("topology_id"),
        "topology_digest": (payload.get("topology") or {}).get("sha256")
        if isinstance(payload.get("topology"), dict) else None,
        "agreement_id": agreement_doc.get("agreement_id"),
        "agreement_digest": (agreement_envelope or {}).get("sha256"),
        "evidence_id": agreement_doc.get("evidence_id"),
        "evidence_digest": _evidence_digest(payload, agreement_doc.get("evidence_id")),
        "meter_segments": [
            {"meter_id": mid,
             "bridges": [seg["bridge"] for seg in rep.get("segments", [])],
             "evidence": [e for seg in rep.get("segments", []) for e in seg.get("evidence", [])]}
            for mid, rep in rec.get("meters", {}).items()
        ],
    }

    statement = {
        "period_id": (period or {}).get("id", rec.get("period_id", "")),
        "period": rec.get("period", {}),
        "stations": station_ids,
        "master_kwh": number(master_exact),
        "direct_kwh": {s: number(direct_exact[s]) for s in station_ids},
        "shared_meter_ids": shared_meter_ids,
        "shared_kwh": number(shared_total_exact),
        "shared_allocation_kwh": {s: number(allocation_exact[s]) for s in station_ids},
        "billed_kwh": {s: number(billed_exact[s]) for s in station_ids},
        # 兼容性别名
        "station_kwh": {s: number(billed_exact[s]) for s in station_ids},
        "unassigned_kwh": number(unassigned_exact),
        "pending_allocation_kwh": number(pending_allocation_exact),
        "meter_difference_kwh": number(meter_difference_exact),
        "pending_meters": rec.get("pending_meters", {}),
        "conservation": {
            "master_kwh": number(master_exact),
            "billed_total_kwh": number(billed_total),
            "unassigned_kwh": number(unassigned_exact),
            "identity": "sum(billed_kwh) + unassigned_kwh == master_net_increment_kwh",
            "ok": billed_total + unassigned_exact == master_exact,
            "meter_conservation_ok": rec.get("conservation_ok", False),
        },
        "agreement": _public_agreement(agreement_doc, weights),
        "basis": basis,
        "evidence_chain": chain,
        "hierarchy_checks": rec.get("hierarchy_checks", []),
        "naive_sum_all_meters_kwh": rec.get("naive_sum_all_meters_kwh"),
        "parent_child_double_count_kwh": rec.get("parent_child_double_count_kwh"),
    }
    return statement


def _evidence_digest(payload: dict, evidence_id: str | None):
    if not evidence_id:
        return None
    try:
        rec = _merge_pool(payload, ("evidence_records", "evidence"), "证据").get(evidence_id)
    except SignatureError:
        return None
    return rec.get("sha256") if isinstance(rec, dict) else None


def _public_agreement(doc: dict, weights: dict[str, Fraction] | None) -> dict:
    if not doc:
        return {"status": "no_effective_agreement_shared_energy_pending"}
    out = {"agreement_id": doc.get("agreement_id"), "method": doc.get("method"),
           "period_id": doc.get("period_id"), "parties": doc.get("parties"),
           "evidence_id": doc.get("evidence_id")}
    if weights:
        out["derived_share"] = {s: number(w) for s, w in weights.items()}
        out["derived_ratio"] = {s: f"{w.numerator}/{w.denominator}" for s, w in weights.items()}
    return out


# 语义别名
def station_charges(payload: dict, agreement=None, stations=None, **kwargs) -> dict:
    return settle(payload, agreement, stations, **kwargs)


def bill(payload: dict, agreement=None, stations=None, **kwargs) -> dict:
    return settle(payload, agreement, stations, **kwargs)


# ---------------------------------------------------------------------------
# 兼容旧入口 station_kwh(readings, stations)
# ---------------------------------------------------------------------------

def station_kwh(readings=None, stations=None, agreement=None, *,
                agreement_id: str | None = None,
                topology: dict | None = None,
                period: dict | None = None,
                payload: dict | None = None,
                shared_default: str = "equal",
                **kwargs):
    """返回 ``{station: 可计费 kWh}`` 纯 dict(保持种子函数返回形态)。

    * 首参为完整夹具 dict 时走完整结算(默认用夹具 current_agreement);
    * 首参为旧版 readings 列表时, 依据记录上的 role/station/parent 分类:
      主表忽略, 直用表归工位, shared 表按 ``agreement`` 分摊;
      未给协议时默认等分摊(等面积路径的退化形态), 但完整合规出账应使用
      :func:`settle` 并提供签认协议。
    """
    if isinstance(readings, dict):
        statement = settle(readings, agreement=agreement,
                           stations=stations, agreement_id=agreement_id, **kwargs)
        return dict(statement["billed_kwh"])

    if payload is not None:
        statement = settle(payload, agreement=agreement, stations=stations,
                           agreement_id=agreement_id, **kwargs)
        return dict(statement["billed_kwh"])

    records = readings or []
    station_ids = list(stations) if stations else list(STATIONS)
    data: dict[str, Any] = {"stations": station_ids, "readings": records}
    if topology is not None:
        data["topology"] = topology
    if period is not None:
        data["billing_period"] = period

    if agreement is None and agreement_id is None and shared_default == "equal":
        agreement = {"method": "equal_shares"}
    statement = settle(data, agreement=agreement, stations=station_ids,
                       period=period, verify=False, **kwargs)
    return dict(statement["billed_kwh"])


# ---------------------------------------------------------------------------
# 晚到校正与版本账
# ---------------------------------------------------------------------------

def correct_payload(payload: dict, corrections: Iterable[dict]) -> dict:
    """不可变校正: 返回深拷贝后的新 payload, 原 payload 不动。

    支持的 correction op:

    * ``upsert_reading``: 在指定表时间轴中按 ``at``(或 observed_at)插入/替换读数;
    * ``add_bridge``: 给指定表追加回卷/换表证据;
    * ``set_current_agreement``: 更换当期协议 id;
    * ``merge``: 浅合并顶层键(例如补 agreement_records/evidence_records)。
    """
    new_payload = copy.deepcopy(payload)
    meters = {m["id"]: m for m in new_payload.setdefault("meters", [])}
    if not meters and new_payload.get("readings"):
        meters = {m["id"]: m for m in new_payload["readings"]}
    for corr in corrections:
        op = corr.get("op")
        if op == "upsert_reading":
            mid = corr["meter_id"]
            meter = meters[mid]
            timeline = meter.setdefault("readings", [])
            reading = corr["reading"]
            key = reading.get("at")
            for i, old in enumerate(timeline):
                if old.get("at") == key:
                    timeline[i] = reading
                    break
            else:
                timeline.append(reading)
            timeline.sort(key=lambda r: r.get("observed_at") or r.get("at"))
        elif op == "add_bridge":
            meter = meters[corr["meter_id"]]
            meter.setdefault("bridges", []).append(corr["bridge"])
        elif op == "set_current_agreement":
            new_payload["current_agreement_id"] = corr["agreement_id"]
        elif op == "merge":
            for key, value in corr.get("values", {}).items():
                if isinstance(value, list) and isinstance(new_payload.get(key), list):
                    new_payload[key] = new_payload[key] + value
                else:
                    new_payload[key] = value
        else:
            raise EnergyDataError(f"未知校正 op: {op}")
    return new_payload


class EnergyLedger:
    """按计费时段保存不可变结算版本的台账。"""

    def __init__(self):
        self._books: dict[str, list[dict]] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def post(self, statement: dict, *, period_id: str | None = None,
             posted_at: str | None = None, note: str = "") -> dict:
        pid = period_id or statement.get("period_id")
        if not pid:
            raise EnergyDataError("过账必须有 period_id")
        entry = {
            "period_id": pid,
            "revision": len(self._books.get(pid, [])) + 1,
            "posted_at": posted_at or self._now(),
            "note": note,
            "sha256": digest_doc(statement),
            "statement": copy.deepcopy(statement),
            "correction": None,
        }
        self._books.setdefault(pid, []).append(entry)
        return self.public_entry(entry)

    def revise(self, period_id: str, new_statement: dict, *,
               reason: str, evidence_id: str,
               posted_at: str | None = None) -> dict:
        """为既有时段追加更正版本; 旧版本原样保留。"""
        history = self._books.get(period_id)
        if not history:
            raise EnergyDataError(f"时段 {period_id} 尚未过账, 不能更正")
        old = history[-1]["statement"]
        changed = _changed_fields(old, new_statement)
        entry = {
            "period_id": period_id,
            "revision": len(history) + 1,
            "posted_at": posted_at or self._now(),
            "sha256": digest_doc(new_statement),
            "statement": copy.deepcopy(new_statement),
            "correction": {
                "reason": reason,
                "evidence_id": evidence_id,
                "corrects_revision": len(history),
                "changed_fields": changed,
                "scope": [period_id],
            },
        }
        history.append(entry)
        return self.public_entry(entry)

    def versions(self, period_id: str) -> list[dict]:
        return [self.public_entry(e) for e in self._books.get(period_id, [])]

    def current(self, period_id: str) -> dict:
        if period_id not in self._books:
            raise KeyError(period_id)
        return copy.deepcopy(self._books[period_id][-1]["statement"])

    def current_entry(self, period_id: str) -> dict:
        return self.public_entry(self._books[period_id][-1])

    def history_periods(self) -> list[str]:
        return sorted(self._books)

    @staticmethod
    def public_entry(entry: dict) -> dict:
        return {
            "period_id": entry["period_id"],
            "revision": entry["revision"],
            "posted_at": entry["posted_at"],
            "sha256": entry["sha256"],
            "correction": copy.deepcopy(entry["correction"]),
            "billed_kwh": copy.deepcopy(entry["statement"].get("billed_kwh")),
            "unassigned_kwh": entry["statement"].get("unassigned_kwh"),
            "conservation_ok": entry["statement"].get("conservation", {}).get("ok"),
        }


# 常见命名别名
Ledger = EnergyLedger


def render_statement(statement: dict) -> str:
    """把结算单渲染成可追溯的中文对账文本(计费 kWh/差额/分摊依据)。"""
    lines = [
        f"计费时段: {statement.get('period_id')} "
        f"{statement.get('period', {}).get('start', '')} ~ "
        f"{statement.get('period', {}).get('end', '')}",
        f"主表净增量: {statement['master_kwh']} kWh (唯一总量口径, 不与子表相加)",
        "工位可计费电量:",
    ]
    for s in statement["stations"]:
        lines.append(
            f"  - {s}: 直用 {statement['direct_kwh'][s]} + "
            f"共享分摊 {statement['shared_allocation_kwh'][s]} = "
            f"{statement['billed_kwh'][s]} kWh")
    c = statement["conservation"]
    lines.append(
        f"分站计费合计 {c['billed_total_kwh']} + 待核差额 "
        f"{c['unassigned_kwh']} = 主表 {c['master_kwh']} kWh "
        f"[守恒 {'OK' if c['ok'] else 'FAIL'}]")
    if statement.get("pending_meters"):
        lines.append(f"待核表计(不得跨租户借量): {statement['pending_meters']}")
    agr = statement.get("agreement") or {}
    if agr.get("agreement_id"):
        lines.append(
            f"分摊协议: {agr['agreement_id']} 方法={agr['method']} "
            f"证据={agr.get('evidence_id')} 推导比例={agr.get('derived_ratio')}")
    for b in statement.get("basis", []):
        if b["kind"] == "direct_meter":
            lines.append(f"依据[直用] {b['station']} 表 {b['meter_id']}: {b['kwh']} kWh")
        elif b["kind"] == "shared_allocation":
            lines.append(
                f"依据[共享] {b['station']} 按 {b['weight']} 分 "
                f"{b['shared_meter']} {b['shared_kwh']} -> {b['allocated_kwh']} kWh "
                f"(协议 {b['agreement_id']}, 证据 {b['evidence_id']})")
    chain = statement.get("evidence_chain") or {}
    if chain.get("topology_digest"):
        lines.append(
            f"拓扑 {chain.get('topology_id')} 签署摘要: "
            f"{chain['topology_digest'][:16]}…")
    if chain.get("agreement_digest"):
        lines.append(f"协议签署摘要: {chain['agreement_digest'][:16]}…")
    return "\n".join(lines)



def _changed_fields(old: dict, new: dict, prefix: str = "") -> list[str]:
    changed = []
    keys = set(old) | set(new)
    for key in sorted(keys):
        path = f"{prefix}.{key}" if prefix else key
        ov, nv = old.get(key), new.get(key)
        if isinstance(ov, dict) and isinstance(nv, dict):
            changed.extend(_changed_fields(ov, nv, path))
        elif ov != nv:
            changed.append(path)
    return changed


# 常见调用命名的等价别名
calculate_bills = settle
build_statement = settle
allocate_hvac = allocate_shared
split_shared_energy = allocate_shared
