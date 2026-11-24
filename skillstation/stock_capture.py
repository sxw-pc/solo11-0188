"""批次级耗材采证：实物件号、质检放行、级别替代与有效期。

旧接口 :func:`available(lots, part)` 仍按 ``part`` 返回仓库总量（向后兼容
``available(lots, "filter") == 10`` 的种子断言）。

传入日期 / 级别 / 质检原件 / 召回 / 已占用件号后，本模块给出的是**批次可用量**
（精确到实物 serial），由 :mod:`skillstation.kit_reservation` 用来驱动跨场排他
预留，而不是再让每场请求独立比较一个总量。

所有函数均为无副作用的纯函数；质检原件（authorization / document）接受 dict
映射或原件列表两种形态。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Sequence

# 级别自低到高。高级件可以顶替低级需求，反之禁止。
DEFAULT_GRADE_ORDER: tuple[str, ...] = ("C", "B", "A")

_BLOCK_WORDS = {
    "quarantine", "quarantined", "recall", "recalled", "block", "blocked",
    "hold", "frozen", "reject", "rejected", "isolated", "seized",
    "隔离", "召回", "封存", "冻结", "扣留", "不合格", "禁放",
}
_RELEASE_WORDS = {
    "release", "released", "accepted", "accept", "approved", "pass", "passed",
    "ok", "available", "cleared", "放行", "已放行", "合格", "验收通过", "可发",
}


# --------------------------------------------------------------------------- #
# 基础值工具
# --------------------------------------------------------------------------- #
def as_date(value: Any) -> date | None:
    """把 ``date`` / ISO 字符串 / ``None`` 统一成 ``date``。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def grade_rank(grade: Any, grade_order: Sequence[str] | None = None) -> int:
    """级别序号，越大越高级。未知名次排在 C 之下（保守，不允许顶替已知高级）。"""
    order = tuple(grade_order) if grade_order else DEFAULT_GRADE_ORDER
    lookup = {str(g).strip().upper(): i for i, g in enumerate(order)}
    key = str(grade or "").strip().upper()
    if key in lookup:
        return lookup[key] + 1
    # 单字母级别按 A-Z 给一个稳定且通常符合直觉的次序（A 最高）。
    if len(key) == 1 and "A" <= key <= "Z":
        return ord(key) - ord("A") + 10
    return 0


def grade_covers(supply_grade: Any, required_grade: Any,
                 grade_order: Sequence[str] | None = None) -> bool:
    """高级批次可覆盖低级需求；低级件不能顶替高级需求。"""
    if required_grade in (None, ""):
        return True
    return grade_rank(supply_grade, grade_order) >= grade_rank(
        required_grade, grade_order
    )


# 中文语义别名。
can_substitute = grade_covers
substitutes = grade_covers


def is_fresh(expiry: Any, use_date: Any) -> bool:
    """批次在实际使用日是否仍在有效期内。

    约定 ``expiry >= use_date``（“有效期至”当日仍可使用）。无有效期的批次
    视为长期有效。
    """
    exp = as_date(expiry)
    used = as_date(use_date)
    if exp is None or used is None:
        return True
    return exp >= used


def expired(expiry: Any, use_date: Any) -> bool:
    return not is_fresh(expiry, use_date)


# --------------------------------------------------------------------------- #
# 实物件号
# --------------------------------------------------------------------------- #
def serials_of(lot: Mapping[str, Any]) -> list[str]:
    """返回一个批次的全部实物件号。

    优先使用 fixture 中显式给出的 ``serials`` / ``units[].serial``；否则按
    ``quantity`` 生成确定性件号 ``"<lot id>-<n>"``。
    """
    if lot.get("serials"):
        return [str(s) for s in lot["serials"]]
    units = lot.get("units")
    if isinstance(units, list) and units:
        out: list[str] = []
        for i, unit in enumerate(units, 1):
            if isinstance(unit, Mapping):
                out.append(str(unit.get("serial") or unit.get("id") or f"{lot.get('id')}-{i}"))
            else:
                out.append(str(unit))
        return out
    qty = int(lot.get("quantity", 0) or 0)
    lot_id = lot.get("id", "LOT")
    return [f"{lot_id}-{i}" for i in range(1, qty + 1)]


# --------------------------------------------------------------------------- #
# 质检原件 / 验收 / 召回状态
# --------------------------------------------------------------------------- #
def classify_decision(value: Any) -> str:
    """把质检原件里的结论归类为 ``released`` / ``blocked`` / ``unknown``。"""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "released" if value else "blocked"
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if text in _BLOCK_WORDS or any(w in text for w in ("quarantin", "recall", "block")):
        return "blocked"
    if text in _RELEASE_WORDS or any(w in text for w in ("releas", "approv", "accept")):
        return "released"
    # 中文包含匹配（lower 不影响中文）。
    for word in _BLOCK_WORDS:
        if word and word in str(value):
            return "blocked"
    for word in _RELEASE_WORDS:
        if word and word in str(value):
            return "released"
    return "unknown"


def normalize_documents(documents: Any) -> dict[str, list[dict[str, Any]]]:
    """把 dict 映射或原件列表归一为 ``{lot_id: [document, ...]}``（幂等）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    if not documents:
        return out

    def _append(lot_key: Any, doc: dict[str, Any]) -> None:
        lot_id = doc.get("lot_id", doc.get("lot", doc.get("batch_id", doc.get("batch", lot_key))))
        out.setdefault(str(lot_id), []).append(doc)

    if isinstance(documents, Mapping):
        # 可能是 {lot_id: "released"}、{lot_id: {...}} 或已归一化的
        # {lot_id: [{...}, ...]}；逐形态展开，保证重复归一不变形。
        for lot_id, value in documents.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping):
                        doc = dict(item)
                        doc.setdefault("lot_id", lot_id)
                        _append(lot_id, doc)
                    else:
                        _append(lot_id, {"lot_id": lot_id, "decision": item})
            elif isinstance(value, Mapping):
                doc = dict(value)
                doc.setdefault("lot_id", lot_id)
                _append(lot_id, doc)
            else:
                _append(lot_id, {"lot_id": lot_id, "decision": value})
        return out
    for raw in documents:
        if not isinstance(raw, Mapping):
            continue
        doc = dict(raw)
        lot_id = doc.get("lot_id", doc.get("lot", doc.get("batch_id", doc.get("batch"))))
        if lot_id is None:
            continue
        _append(lot_id, doc)
    return out


# 兼容旧名。
normalize_authorizations = normalize_documents


def document_state(documents: Iterable[Mapping[str, Any]] | None) -> str:
    """一组同一批次的质检原件的有效结论：任一隔离/召回即 blocked。"""
    state = "unknown"
    for doc in documents or []:
        decision = doc.get("decision", doc.get("status", doc.get("result")))
        kind = classify_decision(decision)
        if kind == "blocked":
            return "blocked"
        if kind == "released":
            state = "released"
    return state


def lot_state(lot: Mapping[str, Any],
              documents: Mapping[str, list[dict[str, Any]]] | None = None,
              *, require_release: bool = False) -> tuple[str, list[str]]:
    """返回批次有效状态及原因。

    优先级：质检/召回原件中的 blocked > 批次自带 status > 放行原件 >
    默认 released（除非 ``require_release=True``，此时无放行原件为 pending）。
    """
    lot_id = str(lot.get("id"))
    reasons: list[str] = []
    docs = (documents or {}).get(lot_id, [])
    doc_kind = document_state(docs)
    if doc_kind == "blocked":
        reasons.append("qc-document-blocked")
        return "blocked", reasons

    own = classify_decision(lot.get("status") or lot.get("state") or lot.get("qc_status"))
    if own == "blocked":
        reasons.append("lot-status-blocked")
        return "blocked", reasons

    if doc_kind == "released" or own == "released":
        return "released", reasons
    if lot.get("released") is True or lot.get("accepted") is True:
        return "released", reasons
    if require_release:
        reasons.append("release-document-required")
        return "pending", reasons
    return "released", reasons


def is_released(lot: Mapping[str, Any], documents: Any = None,
                *, require_release: bool = False) -> bool:
    state, _ = lot_state(
        lot, normalize_documents(documents), require_release=require_release
    )
    return state == "released"


# --------------------------------------------------------------------------- #
# 实物件 / 批次可用量
# --------------------------------------------------------------------------- #
def iter_units(lots: Iterable[Mapping[str, Any]], *,
               documents: Any = None,
               blocked_serials: Iterable[str] | None = None,
               require_release: bool = False,
               grade_order: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """把批次展开成实物件号级别的标准记录。"""
    docs = normalize_documents(documents)
    blocked = {str(s) for s in (blocked_serials or ())}
    units: list[dict[str, Any]] = []
    for lot in lots or []:
        state, reasons = lot_state(lot, docs, require_release=require_release)
        for serial in serials_of(lot):
            unit_blocked = state != "released" or serial in blocked
            unit_reasons = list(reasons)
            if state != "released":
                unit_reasons.append(state)
            if serial in blocked:
                unit_reasons.append("serial-recalled")
            units.append({
                "serial": serial,
                "lot_id": lot.get("id"),
                "part": lot.get("part"),
                "grade": lot.get("grade"),
                "grade_rank": grade_rank(lot.get("grade"), grade_order),
                "expiry": lot.get("expiry"),
                "location": lot.get("location"),
                "lot_status": state,
                "blocked": unit_blocked,
                "reasons": unit_reasons,
            })
    return units


def available(lots: Iterable[Mapping[str, Any]], part: Any = None,
              on_date: Any = None, required_grade: Any = None,
              documents: Any = None, blocked_serials: Iterable[str] | None = None,
              occupied_serials: Iterable[str] | None = None,
              require_release: bool = False,
              grade_order: Sequence[str] | None = None,
              **_extra: Any) -> int:
    """可用数量。

    - 只给 ``part``（旧调用 ``available(lots, "filter")``）：按 part 汇总
      ``quantity``，例如 10。
    - 第二个参数也可以直接是一场请求 dict（自动取 part/date/required_grade）。
    - 给出 ``on_date`` / ``required_grade`` / ``documents`` / ``occupied_serials``
      等任一条件时：返回满足效期、级别、放行且未被占用/召回的**实物件数**。
    """
    if isinstance(part, Mapping):
        request = part
        on_date = on_date if on_date is not None else (
            request.get("date") or request.get("use_date") or request.get("exam_date")
        )
        required_grade = required_grade if required_grade is not None else (
            request.get("required_grade") or request.get("grade")
            or request.get("min_grade")
        )
        part = request.get("part")
    any_condition = (
        on_date is not None or required_grade is not None or documents is not None
        or blocked_serials is not None or occupied_serials is not None
        or require_release
    )
    if not any_condition:
        return sum(
            int(lot.get("quantity", 0) or 0)
            for lot in lots or []
            if part is None or lot.get("part") == part
        )

    occupied = {str(s) for s in (occupied_serials or ())}
    count = 0
    for unit in iter_units(
        lots, documents=documents, blocked_serials=blocked_serials,
        require_release=require_release, grade_order=grade_order,
    ):
        if part is not None and unit["part"] != part:
            continue
        if unit["blocked"]:
            continue
        if unit["serial"] in occupied:
            continue
        if required_grade is not None and not grade_covers(
            unit["grade"], required_grade, grade_order
        ):
            continue
        if on_date is not None and not is_fresh(unit["expiry"], on_date):
            continue
        count += 1
    return count


def batch_availability(lots: Iterable[Mapping[str, Any]],
                       request: Mapping[str, Any] | None = None, *,
                       part: str | None = None, on_date: Any = None,
                       required_grade: Any = None,
                       documents: Any = None,
                       blocked_serials: Iterable[str] | None = None,
                       occupied_serials: Iterable[str] | None = None,
                       require_release: bool = False,
                       grade_order: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """逐批次给出对一场请求的合格/可用实物件号清单。

    ``request`` 可直接传入（读取 part/date/required_grade），也可用关键字
    显式覆盖。返回每个批次一条，包含 ``eligible_serials``（级别+效期+放行
    合格）与 ``available_serials``（再扣除已占用件号）。
    """
    if request is not None:
        part = part if part is not None else request.get("part")
        on_date = on_date if on_date is not None else (
            request.get("date") or request.get("use_date") or request.get("exam_date")
        )
        required_grade = required_grade if required_grade is not None else (
            request.get("required_grade") or request.get("grade") or request.get("min_grade")
        )
    occupied = {str(s) for s in (occupied_serials or ())}
    blocked = {str(s) for s in (blocked_serials or ())}
    docs = normalize_documents(documents)

    result: list[dict[str, Any]] = []
    for lot in lots or []:
        if part is not None and lot.get("part") != part:
            continue
        state, reasons = lot_state(lot, docs, require_release=require_release)
        all_serials = serials_of(lot)
        eligible: list[str] = []
        availability_reasons: dict[str, list[str]] = {}
        for serial in all_serials:
            why: list[str] = []
            if state != "released":
                why.append(state)
            if serial in blocked:
                why.append("recalled")
            if required_grade is not None and not grade_covers(
                lot.get("grade"), required_grade, grade_order
            ):
                why.append("grade-too-low")
            if on_date is not None and not is_fresh(lot.get("expiry"), on_date):
                why.append("expired-on-use-date")
            if why:
                availability_reasons[serial] = why
            else:
                eligible.append(serial)
        free = [s for s in eligible if s not in occupied]
        result.append({
            "lot_id": lot.get("id"),
            "part": lot.get("part"),
            "grade": lot.get("grade"),
            "expiry": lot.get("expiry"),
            "location": lot.get("location"),
            "state": state,
            "quantity": int(lot.get("quantity", len(all_serials)) or len(all_serials)),
            "serials": all_serials,
            "eligible_serials": eligible,
            "available_serials": free,
            "occupied_serials": [s for s in eligible if s in occupied],
            "serial_issues": availability_reasons,
            "reasons": reasons,
        })
    return result


# 常见命名别名，方便不同调用习惯。
availability_by_batch = batch_availability
batches_for = batch_availability
usable_quantity = available
check_availability = batch_availability
lot_availability = batch_availability
batch_available = batch_availability
capture = batch_availability


class StockCapture:
    """批次库存的薄封装：绑定质检原件 / 召回件号后反复查询。"""

    def __init__(self, lots: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
                 *, documents: Any = None, blocked_serials: Iterable[str] | None = None,
                 require_release: bool = False,
                 grade_order: Sequence[str] | None = None):
        if isinstance(lots, Mapping) and "lots" in lots:
            payload = lots
            self.lots = payload.get("lots", [])
            documents = documents if documents is not None else payload.get("qc_documents")
        else:
            self.lots = list(lots or [])
        self.documents = normalize_documents(documents)
        self.blocked_serials = {str(s) for s in (blocked_serials or ())}
        self.require_release = require_release
        self.grade_order = grade_order

    def available(self, part: str | None = None, **kw: Any) -> int:
        kw.setdefault("documents", self.documents)
        kw.setdefault("blocked_serials", self.blocked_serials)
        kw.setdefault("require_release", self.require_release)
        kw.setdefault("grade_order", self.grade_order)
        return available(self.lots, part, **kw)

    def batches(self, request: Mapping[str, Any] | None = None, **kw: Any):
        kw.setdefault("documents", self.documents)
        kw.setdefault("blocked_serials", self.blocked_serials)
        kw.setdefault("require_release", self.require_release)
        kw.setdefault("grade_order", self.grade_order)
        return batch_availability(self.lots, request, **kw)

    def units(self) -> list[dict[str, Any]]:
        return iter_units(
            self.lots, documents=self.documents, blocked_serials=self.blocked_serials,
            require_release=self.require_release, grade_order=self.grade_order,
        )
