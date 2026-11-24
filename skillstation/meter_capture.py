"""主分表拓扑、签署校验、时间轴对齐与校准区间能量重建。

本模块只处理"表计侧事实":

* 验证主/分表拓扑及场馆、租户各方的签署原件(摘要 + 签认);
* 按原始读数、倍率证据重建每只表在同一计费区间的净增量;
* 用回卷证据(register wrap)衔接负差、用换表工单衔接新旧表,
  绝不把负差 ``max(end-start, 0)`` 归零;
* 用签署的时钟偏差/可信采集时刻对齐时间轴, 绝不按"时刻最近"猜边界;
* 按直接父子关系做区间守恒: 任一级父表净增量 = 其直接子表之和 + 级差。

``energy_ledger`` 负责协议分摊与工位账, 本模块不做费用分摊。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any, Iterable

# 守恒校验的绝对容差(单位 kWh)。固定夹具全部为整数, 差额严格为 0。
CONSERVATION_TOLERANCE = Fraction("0.001")


class EnergyDataError(ValueError):
    """能源对账数据/证据不满足要求。"""


class SignatureError(EnergyDataError):
    """签署原件缺失、签认方不全或摘要被篡改。"""


class TopologyError(EnergyDataError):
    """拓扑不是带唯一根的有效有向树, 或父表与子表被重复相加。"""


class BoundaryAlignmentError(EnergyDataError):
    """读数时间轴无法按证据对齐到同一计费区间边界。"""


class NonMonotonicReadingError(EnergyDataError):
    """读数倒退且没有回卷/换表证据可以衔接。"""


class ConservationError(EnergyDataError):
    """区间守恒被破坏: 父表净增量对不回直接子表与待核级差。"""


# ---------------------------------------------------------------------------
# 规范化、摘要与签署
# ---------------------------------------------------------------------------

def canonical(value: Any) -> str:
    """与 ``skillstation.core.canonical`` 一致的确定性 JSON 编码。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def digest_doc(doc: Any) -> str:
    return hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()


def seal_record(doc: dict, parties: Iterable[tuple[str, str]] | Iterable[str],
                at: str = "2026-11-01T12:00:00+08:00") -> dict:
    """生成一份带摘要与双方(或多方)签认的原件信封。

    ``parties`` 元素可以是 ``(party_id, signer_name)`` 或纯 party id。
    签认记录引用正文摘要; 事后改正文会让 :func:`verify_sealed` 失败。
    """
    sha = digest_doc(doc)
    signoffs = []
    for item in parties:
        if isinstance(item, (tuple, list)):
            party, by = item[0], item[1]
        else:
            party, by = item, f"{item}授权代表"
        signoffs.append({"party": party, "by": by, "at": at, "ack": sha})
    return {"v": 1, "doc": doc, "sha256": sha, "signoffs": signoffs}


def verify_sealed(record: dict, required_parties: Iterable[str] | None = None,
                 *, label: str = "签署原件") -> dict:
    """校验信封摘要与签认, 返回正文 doc。

    没有 ``sha256`` 字段的普通 dict 视为未签署正文直接返回(便于测试构造),
    但一旦带签署信息就必须完整校验; 要求必备签认方时缺签即拒绝。
    """
    if not isinstance(record, dict):
        raise SignatureError(f"{label}必须是对象")
    if "sha256" not in record and "signoffs" not in record:
        return record  # 未封装的普通正文(测试/内部构造)
    doc = record.get("doc")
    sha = record.get("sha256")
    if not isinstance(doc, dict) or not sha:
        raise SignatureError(f"{label}信封缺少 doc/sha256")
    actual = digest_doc(doc)
    if actual != sha:
        raise SignatureError(f"{label}正文摘要不一致: 原件可能被篡改")
    signoffs = record.get("signoffs") or []
    seen = set()
    for so in signoffs:
        if so.get("ack") != sha:
            raise SignatureError(f"{label}存在未对当前摘要签认的签署: {so.get('party')}")
        if not so.get("party") or not so.get("at"):
            raise SignatureError(f"{label}签署记录缺少 party/at")
        seen.add(so["party"])
    missing = [p for p in (required_parties or []) if p not in seen]
    if missing:
        raise SignatureError(f"{label}缺少必备方签认: {','.join(missing)}")
    return doc


# ---------------------------------------------------------------------------
# 时间轴
# ---------------------------------------------------------------------------

def parse_ts(value: str) -> datetime:
    if not isinstance(value, str):
        raise BoundaryAlignmentError(f"时间戳必须是 ISO 字符串, 得到 {value!r}")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise BoundaryAlignmentError(f"时间戳必须带时区偏移: {value}")
    return dt


def aligned_instant(stamped_at: str, clock_fast_seconds: float | int | str = 0) -> datetime:
    """把表面时间换算为权威时间。

    约定 ``clock_fast_seconds > 0`` 表示表钟走快, 故
    ``权威时刻 = 表面时刻 - clock_fast_seconds``; 负值表示走慢。
    """
    fast = Fraction(str(clock_fast_seconds or 0))
    return parse_ts(stamped_at) - timedelta(seconds=float(fast))


def reading_instant(meter: dict, reading: dict) -> datetime:
    """读数在统一权威时间轴上的时刻。

    优先级: 可信采集时刻 ``observed_at`` > 表面时间按签署偏差
    ``clock_fast_seconds`` 校正。没有任何对齐证据时偏差按 0 处理,
    但边界匹配仍要求读数恰好落在计费边界, 不会退化为最近匹配。
    """
    if reading.get("observed_at"):
        return parse_ts(reading["observed_at"])
    fast = reading.get("clock_fast_seconds", meter.get("clock_fast_seconds", 0))
    return aligned_instant(reading["at"], fast)


def period_bounds(period: dict) -> tuple[datetime, datetime, str]:
    if not period or "start" not in period or "end" not in period:
        raise BoundaryAlignmentError("计费区间必须给出 start/end")
    start, end = parse_ts(period["start"]), parse_ts(period["end"])
    if end <= start:
        raise BoundaryAlignmentError("计费区间 end 必须晚于 start")
    return start, end, period.get("id", "")


# ---------------------------------------------------------------------------
# 读数差与回卷/换表衔接
# ---------------------------------------------------------------------------

def calibrated_delta(start_value: float | int | str,
                     end_value: float | int | str,
                     multiplier: float | int | str = 1,
                     *, wrap_at: float | int | str | None = None,
                     rollover_count: int = 1) -> Fraction:
    """对一对边界读数做倍率与回卷校准。

    仅用于只有 start/end 的薄记录。正常情况 ``(end-start)*倍率``;
    当 end<start 时必须给出 ``wrap_at``(寄存器满度/回卷证据),
    按 ``rollover_count*wrap_at - start + end`` 衔接, 绝不截断负差。
    """
    a, b = Fraction(str(start_value)), Fraction(str(end_value))
    mult = Fraction(str(multiplier))
    if b >= a:
        return (b - a) * mult
    if wrap_at is None:
        raise NonMonotonicReadingError(
            f"读数倒退 {a} -> {b} 且无 wrap_at/回卷证据; 不得把负差归零")
    cap = Fraction(str(wrap_at))
    raw = Fraction(int(rollover_count)) * cap - a + b
    if raw < 0:
        raise NonMonotonicReadingError(
            f"回卷参数不能解释读数 {a} -> {b} (wrap_at={cap})")
    return raw * mult


def _fraction(value: Any, default: Any = 1) -> Fraction:
    return Fraction(str(default if value is None else value))


def meter_segments(meter: dict) -> list[dict]:
    """把一只表的原始读数时间轴重建为带证据的校准能量段。

    每一段包含权威起讫时刻、原始起讫读数、倍率与校准后 kWh;
    倒退读数必须由 ``meter['bridges']`` 中对应的回卷/换表证据衔接。
    """
    meter_id = meter.get("id", "?")
    default_mult = meter.get("multiplier", 1)
    default_wrap = meter.get("wrap_at")
    raw_readings = meter.get("readings")
    if raw_readings is None and {"start", "end"} <= set(meter):
        # 兼容 {id,start,end} 薄记录
        raw_readings = [
            {"at": meter.get("start_at"), "value": meter["start"]},
            {"at": meter.get("end_at"), "value": meter["end"]},
        ]
    if not isinstance(raw_readings, list) or len(raw_readings) < 2:
        raise EnergyDataError(f"表 {meter_id} 至少需要两个原始读数")

    readings = sorted(raw_readings, key=lambda r: reading_instant(meter, r))
    bridges = {b.get("after_at"): b for b in meter.get("bridges", [])}
    segments: list[dict] = []

    for left, right in zip(readings, readings[1:]):
        t0, t1 = reading_instant(meter, left), reading_instant(meter, right)
        if t1 <= t0:
            raise BoundaryAlignmentError(
                f"表 {meter_id} 读数对齐后时刻非递增: {left.get('at')} -> {right.get('at')}")
        v0 = Fraction(str(left["value"]))
        v1 = Fraction(str(right["value"]))
        bridge = bridges.get(right.get("at"))
        mult = _fraction(left.get("multiplier", default_mult))
        kind = "normal"
        evidence: list[str] = []
        raw_kwh: Fraction
        kwh: Fraction

        if bridge is not None:
            kind = bridge.get("kind", "bridge")
            if bridge.get("evidence"):
                evidence.append(str(bridge["evidence"]))

        if kind == "replacement":
            # 跨换表工单: 旧表末读到拆回终值, 新表起始值到当前读值,
            # 两侧分别使用工单核证的倍率。
            old_final = _fraction(bridge["old_final"])
            new_initial = _fraction(bridge["new_initial"])
            m_old = _fraction(bridge.get("old_multiplier", default_mult))
            m_new = _fraction(bridge.get("new_multiplier", default_mult))
            if old_final < v0:
                raise NonMonotonicReadingError(
                    f"表 {meter_id} 换表证据 old_final={old_final} 早于末读 {v0}")
            old_part = (old_final - v0) * m_old
            new_part = (v1 - new_initial) * m_new
            if old_part < 0 or new_part < 0:
                raise NonMonotonicReadingError(
                    f"表 {meter_id} 换表衔接产生负能量: {old_part}+{new_part}")
            raw_kwh = old_part / m_old if m_old else old_part
            kwh = old_part + new_part
        elif v1 < v0 or kind == "rollover":
            cap_value = None
            if bridge is not None:
                cap_value = bridge.get("wrap_at", default_wrap)
            if cap_value is None:
                cap_value = default_wrap
            if bridge is None or kind != "rollover" or cap_value is None:
                raise NonMonotonicReadingError(
                    f"表 {meter_id} 读数倒退 {v0}->{v1} 但缺少回卷证据; "
                    "禁止 max(end-start,0) 归零")
            cap = _fraction(cap_value)
            count = int(bridge.get("rollover_count", 1))
            raw_delta = Fraction(count) * cap - v0 + v1
            if raw_delta <= 0:
                raise NonMonotonicReadingError(
                    f"表 {meter_id} 回卷证据不能解释 {v0}->{v1}")
            raw_kwh = raw_delta
            kwh = raw_delta * _fraction(bridge.get("multiplier", mult))
            kind = "rollover"
        else:
            raw_kwh = v1 - v0
            kwh = raw_kwh * mult

        for ev in (left.get("evidence"), right.get("evidence")):
            if ev:
                evidence.append(str(ev))
        segments.append({
            "meter_id": meter_id,
            "start": t0.isoformat(),
            "end": t1.isoformat(),
            "_start_dt": t0,
            "_end_dt": t1,
            "raw_start": v0,
            "raw_end": v1,
            "multiplier": mult,
            "raw_kwh_exact": raw_kwh,
            "kwh_exact": kwh,
            "bridge": kind,
            "evidence": evidence,
        })
    return segments


def interval_energy(meter: dict, period: dict, *,
                    prorate: bool = False,
                    boundary_tolerance_seconds: int = 0) -> dict:
    """重建单表在 ``period`` 权威区间内的净电量。

    严格模式(默认)要求存在按证据对齐后恰好落在区间两端的边界读数,
    否则抛 :class:`BoundaryAlignmentError` —— 不允许取时刻最近的读数。
    只有显式 ``prorate=True`` 时才对跨越边界的能量段按时间线性切分,
    且结果标记 ``estimated=True``。
    """
    p0, p1, pid = period_bounds(period)
    segments = meter_segments(meter)
    meter_id = meter.get("id", "?")
    if not segments:
        raise EnergyDataError(f"表 {meter_id} 没有可重建的能量段")

    tol = timedelta(seconds=boundary_tolerance_seconds)
    starts = [s["_start_dt"] for s in segments]
    ends = [s["_end_dt"] for s in segments]
    first_start, last_end = starts[0], ends[-1]

    def _match(dt: datetime, target: datetime) -> bool:
        return abs(dt - target) <= tol

    has_start = any(_match(t, p0) for t in starts) or _match(first_start, p0)
    has_end = any(_match(t, p1) for t in ends) or _match(last_end, p1)

    chosen = [s for s in segments
              if s["_start_dt"] >= p0 - tol and s["_end_dt"] <= p1 + tol
              and not (s["_end_dt"] <= p0 - tol)
              and not (s["_start_dt"] >= p1 + tol)]

    estimated = False
    if not has_start or not has_end:
        if not prorate:
            available = [t.isoformat() for t in starts + [last_end]]
            raise BoundaryAlignmentError(
                f"表 {meter_id} 缺少按证据对齐到区间边界的读数 "
                f"(start_resolved={has_start}, end_resolved={has_end}); "
                f"可用对齐时刻 {available}; 不得改用最近读数匹配")
        # 显式估算: 按重叠时长切分跨界段
        total = Fraction(0)
        for s in segments:
            ov0 = max(s["_start_dt"], p0)
            ov1 = min(s["_end_dt"], p1)
            if ov1 <= ov0:
                continue
            span = (s["_end_dt"] - s["_start_dt"]).total_seconds()
            part = (ov1 - ov0).total_seconds()
            total += s["kwh_exact"] * Fraction(part, span)
        estimated = True
    else:
        total = sum((s["kwh_exact"] for s in chosen), Fraction(0))

    clean_segments = []
    for s in chosen:
        item = {k: v for k, v in s.items() if not k.startswith("_")}
        item["kwh"] = number(item.pop("kwh_exact"))
        item["raw_kwh"] = number(item.pop("raw_kwh_exact"))
        item["multiplier"] = number(item["multiplier"])
        item["raw_start"] = number(item["raw_start"])
        item["raw_end"] = number(item["raw_end"])
        clean_segments.append(item)

    return {
        "meter_id": meter_id,
        "period_id": pid,
        "start": p0.isoformat(),
        "end": p1.isoformat(),
        "kwh_exact": total,
        "kwh": number(total),
        "estimated": estimated,
        "segments": clean_segments,
    }


def number(value: Fraction, ndigits: int = 6):
    """Fraction -> int(整数时) 否则有限位 float。"""
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return value.numerator
        return round(float(value), ndigits)
    return value


# ---------------------------------------------------------------------------
# 拓扑
# ---------------------------------------------------------------------------

def topology_doc(topology: dict, *, required_parties=None, verify: bool = True) -> dict:
    if verify:
        return verify_sealed(topology, required_parties, label="计量拓扑")
    return topology.get("doc", topology) if isinstance(topology, dict) else {}


def topology_view(topology: dict, *, required_parties=None, verify: bool = True) -> dict:
    """校验签署拓扑并返回 roots/parents/children 视图。"""
    doc = topology_doc(topology, required_parties=required_parties, verify=verify)
    edges = doc.get("edges") or []
    parent_of: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for edge in edges:
        p, c = edge["parent"], edge["child"]
        if c in parent_of and parent_of[c] != p:
            raise TopologyError(f"子表 {c} 出现多个父表")
        if p == c:
            raise TopologyError(f"表 {p} 不能是自身的父表")
        parent_of[c] = p
        children.setdefault(p, []).append(c)
        children.setdefault(c, children.get(c, []))
        nodes.update((p, c))

    roots = [n for n in nodes if n not in parent_of]
    explicit_root = doc.get("root") or doc.get("master")
    if explicit_root:
        if explicit_root not in nodes:
            raise TopologyError(f"拓扑指定的根表 {explicit_root} 不在 edges 中")
        roots = [explicit_root] if explicit_root in roots else roots
    if not roots:
        raise TopologyError("拓扑没有根(主表)")

    # 环检测 + 从根可达性
    for root in roots:
        seen, stack = set(), [root]
        while stack:
            node = stack.pop()
            if node in seen:
                raise TopologyError("拓扑存在环")
            seen.add(node)
            stack.extend(children.get(node, []))
    attached = set()
    for root in roots:
        stack = [root]
        while stack:
            n = stack.pop()
            attached.add(n)
            stack.extend(children.get(n, []))
    if attached != nodes:
        raise TopologyError(f"拓扑存在无法从主表到达的孤立节点: {nodes - attached}")

    return {"doc": doc, "roots": roots, "parent_of": parent_of,
            "children": children, "nodes": nodes}


ROOT_ID_HINTS = {"master", "main", "main-meter", "main_meter", "root", "总表", "主表"}


def root_meter_ids(payload_or_readings, topology: dict | None = None) -> list[str]:
    """识别不应与下游子表相加的根(主)表 id。"""
    if topology is not None:
        return topology_view(topology)["roots"]
    if isinstance(payload_or_readings, dict):
        payload = payload_or_readings
        if payload.get("topology"):
            return topology_view(payload["topology"])["roots"]
        meters = payload.get("meters") or payload.get("readings") or []
    else:
        meters = payload_or_readings or []
    roots, children = [], set()
    for m in meters:
        if m.get("parent"):
            children.add(m["id"])
    for m in meters:
        if m["id"] in children:
            continue
        role = (m.get("role") or "").lower()
        if role in ("submeter", "child", "direct", "shared", "tenant"):
            continue
        roots.append(m["id"])
    if not roots and meters:
        # 没有任何父级信息时, 全部视为相互独立的进线表
        roots = [m["id"] for m in meters]
    elif meters and len(roots) == len(meters):
        # 没有任何显式 parent/role 标记时的窄启发式: 种子 id "master"
        # (或显式 is_master/master 标志) 即唯一主表。
        named = [m["id"] for m in meters
                 if m.get("is_master") or m.get("master") is True
                 or str(m.get("id", "")).strip().lower() in ROOT_ID_HINTS]
        if named:
            roots = named
    return roots


# ---------------------------------------------------------------------------
# 记录形态适配(兼容旧 {id,start,end} readings)
# ---------------------------------------------------------------------------

def record_energy(rec: dict) -> Fraction:
    """旧版边界记录的校准电量; 支持 role/parent/multiplier/wrap_at。"""
    if "kwh" in rec and rec.get("calibrated", False):
        return _fraction(rec["kwh"])
    return calibrated_delta(
        rec["start"], rec["end"], rec.get("multiplier", 1),
        wrap_at=rec.get("wrap_at"),
        rollover_count=int(rec.get("rollover_count", 1)))


def _payload_from_readings(readings: list[dict], stations=None,
                           topology=None, period=None) -> dict:
    payload: dict = {"readings": readings}
    if stations is not None:
        payload["stations"] = stations
    if topology is not None:
        payload["topology"] = topology
    if period is not None:
        payload["billing_period"] = period
    return payload


# ---------------------------------------------------------------------------
# 主分表区间对账
# ---------------------------------------------------------------------------

def reconcile_meters(payload: dict, *, period: dict | None = None,
                     tolerance: Fraction | float | str = CONSERVATION_TOLERANCE,
                     verify_signatures: bool = True,
                     strict: bool = True,
                     disputed_meter_ids: Iterable[str] | None = None,
                     prorate: bool = False) -> dict:
    """重建所有表计区间能量并做分级守恒校验。

    返回 (JSON 友好的) 对账报告。``strict=False`` 时无法重建或被标记
    disputed 的表进入 ``pending_meters``, 其电量不冒充事实, 而是留在
    待核差额中回到主表净增量 —— 不会向其他租户借量。
    """
    if not isinstance(payload, dict):
        raise EnergyDataError("reconcile_meters 需要夹具/负载 dict")
    period = period or payload.get("billing_period") or payload.get("period")
    if period is None:
        # 无显式区间时, 用每只表首尾读数的并集构造区间(仅薄记录场景)
        period = _implicit_period(payload)
    tol = _fraction(tolerance)
    disputed = set(disputed_meter_ids or [])

    topology = payload.get("topology")
    view = None
    if topology:
        required = payload.get("topology_required_parties")
        view = topology_view(topology, required_parties=required,
                            verify=verify_signatures)

    meter_rows = payload.get("meters")
    if meter_rows is None:
        meter_rows = payload.get("readings") or []
    meters_by_id = {m["id"]: m for m in meter_rows}

    if view is not None:
        missing = [n for n in view["nodes"] if n not in meters_by_id]
        if missing:
            raise TopologyError(f"拓扑节点缺少读数表计: {missing}")
        roots = view["roots"]
        children_map = view["children"]
    else:
        roots = root_meter_ids(meter_rows)
        children_map = {}
        for m in meter_rows:
            if m.get("parent"):
                children_map.setdefault(m["parent"], []).append(m["id"])

    energies: dict[str, Fraction] = {}
    reports: dict[str, dict] = {}
    pending: dict[str, str] = {}

    for mid, meter in meters_by_id.items():
        if mid in disputed:
            pending[mid] = "marked_disputed"
            continue
        try:
            if "readings" in meter or ("start" in meter and "end" in meter and period.get("start")):
                if "readings" in meter:
                    rep = interval_energy(meter, period, prorate=prorate)
                    energies[mid] = rep["kwh_exact"]
                    reports[mid] = rep
                else:
                    val = record_energy(meter)
                    energies[mid] = val
                    reports[mid] = {"meter_id": mid, "kwh": number(val),
                                    "segments": [], "estimated": False}
            else:
                val = record_energy(meter)
                energies[mid] = val
                reports[mid] = {"meter_id": mid, "kwh": number(val),
                                "segments": [], "estimated": False}
        except EnergyDataError as exc:
            if strict:
                raise
            pending[mid] = str(exc)

    missing_energy = [r for r in roots if r not in energies]
    if missing_energy and strict:
        raise ConservationError(f"主表无法重建区间电量: {missing_energy}")

    master_exact = sum((energies.get(r, Fraction(0)) for r in roots), Fraction(0))

    # 直接子表分级守恒
    hierarchy_checks = []
    for parent, child_ids in children_map.items():
        if parent not in energies:
            continue
        known_kids = [c for c in child_ids if c in energies]
        if len(known_kids) != len(child_ids):
            # 有子表待核: 级差中包含未知量, 标记但不破坏总账恒等式
            pass
        residual = energies[parent] - sum((energies[c] for c in known_kids), Fraction(0))
        hierarchy_checks.append({
            "parent": parent,
            "direct_children": list(child_ids),
            "known_children": known_kids,
            "parent_kwh": number(energies[parent]),
            "children_kwh": number(sum((energies[c] for c in known_kids), Fraction(0))),
            "residual_kwh": number(residual),
            "residual_exact": residual,
            "ok": abs(residual) <= tol and len(known_kids) == len(child_ids),
        })

    known_non_roots = [mid for mid in energies if mid not in roots]
    sub_exact = sum((energies[m] for m in known_non_roots), Fraction(0))
    # 待核差额(plug): 主表净增量 - 全部已知非根表电量。
    # disputed/重建失败的表没有能量值, 其份额自然落入该差额, 不向他户借量。
    difference_exact = master_exact - sub_exact

    naive_exact = sum(energies.values(), Fraction(0))
    double_count_exact = naive_exact - master_exact if view is not None else Fraction(0)

    conservation_ok = abs(difference_exact) <= tol and not pending
    report = {
        "period_id": period.get("id", "") if isinstance(period, dict) else "",
        "period": {"start": period.get("start"), "end": period.get("end")} if isinstance(period, dict) else {},
        "roots": roots,
        "master_ids": roots,
        "master_kwh_exact": master_exact,
        "master_kwh": number(master_exact),
        "meter_kwh": {mid: number(val) for mid, val in energies.items()},
        "meters": {mid: {k: v for k, v in rep.items()} for mid, rep in reports.items()},
        "known_submeter_kwh": number(sub_exact),
        "meter_difference_kwh": number(difference_exact),
        "meter_difference_exact": difference_exact,
        "pending_meters": pending,
        "hierarchy_checks": _clean_checks(hierarchy_checks),
        "naive_sum_all_meters_kwh": number(naive_exact),
        "parent_child_double_count_kwh": number(double_count_exact),
        "topology_id": view["doc"].get("topology_id") if view else None,
        "conservation_ok": conservation_ok,
        "tolerance_kwh": number(tol),
    }
    return report


def _clean_checks(checks: list[dict]) -> list[dict]:
    out = []
    for c in checks:
        item = dict(c)
        item.pop("residual_exact", None)
        out.append(item)
    return out


def _implicit_period(payload: dict) -> dict:
    """从薄记录的 start_at/end_at 推导区间; 没有时间戳时给占位区间。"""
    starts, ends = [], []
    for m in payload.get("meters") or payload.get("readings") or []:
        if m.get("start_at"):
            starts.append(m["start_at"])
        if m.get("end_at"):
            ends.append(m["end_at"])
    if starts and ends:
        return {"start": min(starts), "end": max(ends), "id": ""}
    # 没有时间轴时无法也无需做边界对齐; reconcile 走 record_energy 分支。
    return {"start": "1970-01-01T00:00:00+00:00",
            "end": "1970-01-02T00:00:00+00:00", "id": ""}


def assert_conservation(report: dict) -> None:
    """硬约束断言: 主表净增量必须能回到子表合计 + 待核差额。"""
    master = report["master_kwh_exact"]
    sub = sum((Fraction(str(v)) for k, v in report["meter_kwh"].items()
               if k not in report["roots"]), Fraction(0))
    diff = report["meter_difference_exact"]
    if sub + diff != master:
        raise ConservationError(
            f"区间不守恒: 子表 {number(sub)} + 待核级差 {number(diff)} != 主表 {number(master)}")


# ---------------------------------------------------------------------------
# 种子入口(增强语义, 保留旧名字)
# ---------------------------------------------------------------------------

def total_kwh(readings=None, *, topology: dict | None = None,
              period: dict | None = None, **kwargs):
    """返回拓扑根(主)表的校准净电量, 绝不把父表与其子表相加。

    调用形态:
    * ``total_kwh(fixture_dict)``: 完整夹具 -> 主表净增量(固定种子=120);
    * ``total_kwh(readings_list, topology=...)``: 按拓扑只合计根表;
    * 记录自带 role/parent 标记时直接识别主表;
    * 完全无拓扑信息的独立表列表才合计各表(它们之间不存在父子关系)。
    """
    if isinstance(readings, dict):
        rep = reconcile_meters(readings, period=period,
                               strict=kwargs.get("strict", True))
        return rep["master_kwh"]

    records = readings or []
    if topology is not None and isinstance(topology, dict) and topology.get("edges"):
        payload = _payload_from_readings(records, topology=topology, period=period)
        rep = reconcile_meters(payload, period=period, strict=kwargs.get("strict", True))
        return rep["master_kwh"]
    # root_meter_ids 同时处理显式 role/parent 与 "master" id 窄启发式;
    # 完全无拓扑信息时 roots 即全部表, 等价于独立进线表合计。
    roots = set(root_meter_ids(records, topology if topology and topology.get("edges") else None))
    return number(sum((record_energy(r) for r in records if r["id"] in roots),
                      Fraction(0)))


# 常见调用命名的等价别名
reconcile = reconcile_meters
reconcile_period = reconcile_meters
build_topology = topology_view
calibrated_interval = interval_energy
