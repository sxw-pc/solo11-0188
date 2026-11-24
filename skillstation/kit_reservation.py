"""跨场实物预留联合账。

旧实现对每场请求独立比较 part 总量，于是两场都得到 True，却没有任何批次/件号
占用。本模块把以下五本账合成一本可审计的预留账：

1. 批次验收（QC 放行 / 隔离 / 召回原件，见 :mod:`stock_capture`）；
2. 实物件数（精确到 serial，同一件号跨场排他，批次合计不得超库存）；
3. 级别替代（高级可覆盖低级，低级不能顶高级）；
4. 有效期（以每场实际使用日判断，过期件不可发、不可作替代品）；
5. 预装考包（pack / sign / open 历史不可改写；召回追到具体考包并生成
   撤回/替换流转，原预留与签收历史保留）。

对外最常用的是 :class:`ReservationLedger` 与向后兼容的 :func:`reserve`。
``reserve(lots, requests)`` 返回的结果仍是 ``{exam_id: True/False}`` 兼容字典
（值对象与 ``True`` 相等），同时携带逐场批次清单、可发/缺口与采购移库单。
"""
from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any, Mapping

from . import stock_capture as sc

ACTIVE_KIT_STATUSES = {"packed", "signed", "opened", "partially_withdrawn"}


class ReservationError(ValueError):
    """预留账基础异常。"""


class SerialConflict(ReservationError):
    """同一实物件号被预留给第二场。"""


class SerialUnavailable(ReservationError):
    """件号不存在、未放行、被隔离/召回或不满足级别效期。"""


class KitError(ReservationError):
    """考包状态流转非法。"""


def _natural_key(value: Any) -> list[Any]:
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", str(value))]


def _today() -> str:
    return date.today().isoformat()


def _norm_request(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    req = dict(raw)
    req["id"] = str(raw.get("id") or raw.get("exam_id") or f"exam-{index + 1}")
    req["quantity"] = int(raw.get("quantity", raw.get("qty", 0)) or 0)
    req["use_date"] = (
        raw.get("use_date") or raw.get("date") or raw.get("exam_date")
    )
    req["required_grade"] = (
        raw.get("required_grade") or raw.get("grade") or raw.get("min_grade")
    )
    req["location"] = raw.get("location") or raw.get("site")
    req.setdefault("part", "filter")
    return req


class _CallableList(list):
    """既是 list，也允许无参“方法式”调用，兼容 .serials / .serials() 习惯。"""

    def __call__(self, *_args: Any) -> "_CallableList":
        return self


class ExamReservation(dict):
    """单场预留结果。

    作为 dict 可直接取丰富字段；同时 ``exam_result == True/False`` 反映该场
    是否足额可发，兼容旧的 ``{exam_id: bool}`` 断言。
    """

    def __init__(self, payload: Mapping[str, Any]):
        super().__init__(payload)

    def __getattr__(self, name: str) -> Any:
        # 仅在普通属性查找失败时触发：让 res.serials / res.lot_id 等也可用。
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def feasible(self) -> bool:
        return self["shippable_quantity"] >= self["requested_quantity"]

    def __bool__(self) -> bool:  # pragma: no cover - 简单委托
        return self.feasible

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self.feasible is other
        return super().__eq__(other)

    def __repr__(self) -> str:  # pragma: no cover - 调试输出
        return (f"<ExamReservation {self.get('exam_id')} "
                f"shippable={self.get('shippable_quantity')}/"
                f"{self.get('requested_quantity')}>")


class ReservationResult(dict):
    """``{exam_id: ExamReservation}``；聚合账目通过属性或虚拟键访问。"""

    _VIRTUAL_KEYS = {
        "orders", "all_orders", "procurement_orders", "purchase_orders",
        "transfer_orders", "gaps", "purchase_gap", "procurement_gap",
        "procurement_summary", "kits", "kit_list", "events", "workflow",
        "workflow_records", "recalls", "recall_cases", "lot_usage",
        "batch_totals", "lot_balance", "summary", "ledger_report",
        "withdrawals", "replacements",
    }

    def __init__(self, ledger: "ReservationLedger", values: Mapping[str, ExamReservation]):
        super().__init__(values)
        self.ledger = ledger

    def __getattr__(self, name: str) -> Any:
        if name in self._VIRTUAL_KEYS:
            return self._virtual(name)
        # 其余方法/属性（pack_kit、sign_kit、recall_lot、assert_exclusive…）
        # 直接委托给台账，使 reserve(...) 的结果也能驱动后续流转。
        ledger = self.__dict__.get("ledger")
        if ledger is not None and hasattr(ledger, name):
            return getattr(ledger, name)
        raise AttributeError(name)

    # -- 虚拟聚合键：不放进真实 dict 条目，以保持与 {exam: bool} 相等 ------- #
    def __contains__(self, key: object) -> bool:
        if isinstance(key, str) and key in self._VIRTUAL_KEYS:
            return True
        return super().__contains__(key)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str) and not super().__contains__(key) and key in self._VIRTUAL_KEYS:
            return self._virtual(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        if isinstance(key, str) and key in self._VIRTUAL_KEYS and not super().__contains__(key):
            return self._virtual(key)
        return super().get(key, default)

    def _virtual(self, key: str) -> Any:
        ledger = self.ledger
        mapping = {
            "orders": ledger.orders,
            "all_orders": ledger.orders,
            "procurement_orders": ledger.procurement_orders(),
            "purchase_orders": ledger.purchase_orders(),
            "transfer_orders": ledger.transfer_orders(),
            "gaps": ledger.procurement_orders(),
            "purchase_gap": ledger.procurement_orders(),
            "procurement_gap": ledger.procurement_orders(),
            "procurement_summary": ledger.procurement_summary(),
            "kits": ledger.kits,
            "kit_list": ledger.kit_list(),
            "events": ledger.workflow,
            "workflow": ledger.workflow,
            "workflow_records": ledger.workflow,
            "recalls": ledger.recall_reports,
            "recall_cases": ledger.recall_reports,
            "lot_usage": ledger.batch_totals(),
            "batch_totals": ledger.batch_totals(),
            "lot_balance": ledger.lot_balance(),
            "summary": ledger.summary(),
            "ledger_report": ledger.report(),
            "withdrawals": ledger.withdrawals,
            "replacements": ledger.replacements,
        }
        return mapping[key]

    # -- 便捷方法 ---------------------------------------------------------- #
    def flags(self) -> dict[str, bool]:
        return {exam_id: bool(res) for exam_id, res in self.items()}

    def batch_totals(self) -> dict[Any, int]:
        return self.ledger.batch_totals()

    def procurement_gap(self) -> list[dict[str, Any]]:
        return self.ledger.procurement_orders()

    def report(self) -> dict[str, Any]:
        return self.ledger.report()


class ReservationLedger:
    """五账合一的联合预留账（有状态：锁件、考包、签收、召回都留痕）。"""

    def __init__(self,
                 lots: list[Mapping[str, Any]] | Mapping[str, Any] | None = None,
                 requests: list[Mapping[str, Any]] | None = None,
                 *,
                 stock: Any = None,
                 documents: Any = None,
                 authorizations: Any = None,
                 recalls: Any = None,
                 kits: list[Mapping[str, Any]] | Mapping[str, Any] | None = None,
                 blocked_serials: list[str] | set[str] | None = None,
                 require_release: bool = False,
                 grade_order: list[str] | tuple[str, ...] | None = None):
        if lots is None:
            lots = stock
        # 允许直接把解析后的 fixtures/kit-stock.json 整个传入。
        if isinstance(lots, Mapping) and ("lots" in lots or "stock" in lots):
            payload = lots
            self.lots: list[dict[str, Any]] = list(payload.get("lots")
                                                   or payload.get("stock") or [])
            requests = requests if requests is not None else payload.get("requests")
            documents = documents if documents is not None else (
                payload.get("qc_documents") or payload.get("documents")
                or payload.get("authorizations")
            )
            # 注意：``recall_originals`` 是“另一条质检原件路径”的证据，需调用方
            # 显式作为 authorizations/recalls 传入才生效，不能与放行证明同时默认入账。
            recalls = recalls if recalls is not None else payload.get("recalls")
            kits = kits if kits is not None else payload.get("kits")
        else:
            self.lots = list(lots or [])

        self.grade_order = grade_order
        self.require_release = require_release

        # 质检原件 + 召回原件统一归一成 lot_id -> [doc, ...]。
        self.documents: dict[str, list[dict[str, Any]]] = sc.normalize_documents(documents)
        extra_docs = sc.normalize_documents(authorizations)
        for lot_id, docs in extra_docs.items():
            self.documents.setdefault(lot_id, []).extend(docs)
        recall_docs = sc.normalize_documents(recalls)
        for lot_id, docs in recall_docs.items():
            for doc in docs:
                doc.setdefault("decision", "recalled")
            self.documents.setdefault(lot_id, []).extend(docs)

        self.blocked_serials: set[str] = {str(s) for s in (blocked_serials or ())}
        # 召回原件中显式列出的件号也要封。
        for docs in self.documents.values():
            for doc in docs:
                for serial in doc.get("serials", []) or []:
                    self.blocked_serials.add(str(serial))

        self.requests: list[dict[str, Any]] = [
            _norm_request(raw, i) for i, raw in enumerate(requests or [])
        ]
        self.request_by_id = {req["id"]: req for req in self.requests}

        self.preassigned: dict[str, list[str]] = {}
        self.allocations: dict[str, list[dict[str, Any]]] = {}
        self.serial_assignment: dict[str, str] = {}
        self.orders: list[dict[str, Any]] = []
        self.kits: dict[str, dict[str, Any]] = {}
        self.workflow: list[dict[str, Any]] = []
        self.recall_reports: list[dict[str, Any]] = []
        self._seq: dict[str, int] = {}

        if kits:
            kit_iter = kits.values() if isinstance(kits, Mapping) else kits
            for raw_kit in kit_iter:
                self._ingest_kit(raw_kit)

        if self.requests:
            self.plan()

    # ------------------------------------------------------------------ #
    # 基础索引
    # ------------------------------------------------------------------ #
    def _next_id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}-{self._seq[prefix]}"

    def _units(self) -> list[dict[str, Any]]:
        return sc.iter_units(
            self.lots,
            documents=self.documents,
            blocked_serials=self.blocked_serials,
            require_release=self.require_release,
            grade_order=self.grade_order,
        )

    def _unit_index(self) -> dict[str, dict[str, Any]]:
        return {u["serial"]: u for u in self._units()}

    def _lot_by_id(self, lot_id: Any) -> Mapping[str, Any] | None:
        return next((lot for lot in self.lots if lot.get("id") == lot_id), None)

    def _active_kits(self) -> list[dict[str, Any]]:
        return [k for k in self.kits.values() if k.get("status") in ACTIVE_KIT_STATUSES]

    def _kit_held_serials(self) -> dict[str, str]:
        """件号 -> 考包 id（仅当前仍占用的件号；已撤回件号除外）。"""
        held: dict[str, str] = {}
        for kit in self._active_kits():
            withdrawn = set(kit.get("withdrawn_serials", []))
            for serial in kit.get("serials", []):
                if serial in withdrawn:
                    continue
                held[str(serial)] = kit["id"]
        return held

    def _owner_of(self, serial: str) -> str | None:
        if serial in self.serial_assignment:
            return f"exam:{self.serial_assignment[serial]}"
        for exam_id, serials in self.preassigned.items():
            if serial in serials:
                return f"lock:{exam_id}"
        held = self._kit_held_serials()
        if serial in held:
            return f"kit:{held[serial]}"
        return None

    # ------------------------------------------------------------------ #
    # 显式锁实物件号
    # ------------------------------------------------------------------ #
    def lock_serials(self, exam_id: str, serials: list[str], *,
                     replace: bool = False) -> ExamReservation:
        if exam_id not in self.request_by_id:
            raise ReservationError(f"未知场次: {exam_id}")
        req = self.request_by_id[exam_id]
        index = self._unit_index()
        wanted = [str(s) for s in serials]

        for serial in wanted:
            unit = index.get(serial)
            if unit is None:
                raise SerialUnavailable(f"实物件号不存在: {serial}")
            if unit["blocked"]:
                raise SerialUnavailable(
                    f"件号 {serial} 不可用（{','.join(unit['reasons']) or '未放行'}）"
                )
            if req.get("part") and unit["part"] != req["part"]:
                raise SerialUnavailable(f"件号 {serial} 不是 {req['part']}")
            if not sc.grade_covers(unit["grade"], req["required_grade"], self.grade_order):
                raise SerialUnavailable(
                    f"{unit['grade']} 级件 {serial} 不能顶替 {req['required_grade']} 级需求"
                )
            if req.get("use_date") and not sc.is_fresh(unit["expiry"], req["use_date"]):
                raise SerialUnavailable(
                    f"件号 {serial} 有效期 {unit['expiry']} 在使用日 {req['use_date']} 已过期"
                )
            owner = self._owner_of(serial)
            if owner and owner not in (f"exam:{exam_id}", f"lock:{exam_id}"):
                raise SerialConflict(f"件号 {serial} 已被 {owner} 占用，不能再锁给 {exam_id}")

        if replace:
            self.preassigned[exam_id] = list(dict.fromkeys(wanted))
        else:
            merged = self.preassigned.get(exam_id, []) + wanted
            self.preassigned[exam_id] = list(dict.fromkeys(merged))
        result = self.plan()
        return result[exam_id]

    # ------------------------------------------------------------------ #
    # 跨场分配求解（MRV：最受限场次先选；级别刚好优先 + FEFO 近效期优先）
    # ------------------------------------------------------------------ #
    def plan(self, requests: list[Mapping[str, Any]] | None = None) -> ReservationResult:
        if requests is not None:
            self.requests = [_norm_request(raw, i) for i, raw in enumerate(requests)]
            self.request_by_id = {req["id"]: req for req in self.requests}

        units = self._units()
        unit_by_serial = {u["serial"]: u for u in units}

        # 固定占用：显式锁件 + 仍有效的预装/签收考包。
        fixed: dict[str, list[str]] = {}
        owner: dict[str, str] = {}

        def _hold(exam_id: str, serial: str, origin: str) -> None:
            previous = owner.get(serial)
            if previous and previous != exam_id:
                raise SerialConflict(
                    f"实物件号 {serial} 在 {origin} 中与另一场 {previous} 冲突"
                )
            owner[serial] = exam_id
            fixed.setdefault(exam_id, [])
            if serial not in fixed[exam_id]:
                fixed[exam_id].append(serial)

        for exam_id, serials in self.preassigned.items():
            for serial in serials:
                if serial in self.blocked_serials:
                    # 召回后旧锁自动失效，交给缺口/采购流程。
                    continue
                _hold(exam_id, serial, "explicit-lock")
        for kit in self._active_kits():
            withdrawn = set(kit.get("withdrawn_serials", []))
            for serial in kit.get("serials", []):
                if serial in withdrawn:
                    continue
                unit = unit_by_serial.get(str(serial))
                if unit is not None and unit["blocked"]:
                    raise SerialUnavailable(
                        f"考包 {kit['id']} 的件号 {serial} 已被隔离/召回，"
                        f"请用 recall_lot() 生成撤回/替换流转"
                    )
                _hold(str(kit["exam_id"]), str(serial), f"kit {kit['id']}")

        pool = [u for u in units if not u["blocked"] and u["serial"] not in owner]
        chosen: dict[str, list[str]] = {exam_id: list(serials)
                                        for exam_id, serials in fixed.items()}

        def eligible(unit: Mapping[str, Any], req: Mapping[str, Any]) -> bool:
            if req.get("part") and unit["part"] != req["part"]:
                return False
            if not sc.grade_covers(unit["grade"], req["required_grade"], self.grade_order):
                return False
            if req.get("use_date") and not sc.is_fresh(unit["expiry"], req["use_date"]):
                return False
            return True

        remaining = {
            req["id"]: max(0, req["quantity"] - len(chosen.get(req["id"], [])))
            for req in self.requests
        }

        # MRV 迭代：候选最少 / 裕量最小的场次先拿件，避免早到的低级场抢走
        # 未来只能用高级件的批次（贪心反例的修正）。
        while True:
            candidate_map: dict[str, list[dict[str, Any]]] = {}
            for req in self.requests:
                exam_id = req["id"]
                if remaining.get(exam_id, 0) <= 0:
                    continue
                cands = [u for u in pool if eligible(u, req)]
                if cands:
                    candidate_map[exam_id] = cands
            if not candidate_map:
                break

            def scarcity(item: tuple[str, list[dict[str, Any]]]) -> tuple[Any, ...]:
                exam_id, cands = item
                req = self.request_by_id[exam_id]
                need = remaining[exam_id]
                return (
                    len(cands) - need,          # 裕量越小越优先
                    len(cands),                 # 候选越少越优先
                    -sc.grade_rank(req["required_grade"], self.grade_order),
                    str(req.get("use_date") or "9999-12-31"),
                    self.requests.index(req),
                )

            exam_id, cands = min(candidate_map.items(), key=scarcity)
            req = self.request_by_id[exam_id]
            need = remaining[exam_id]

            def unit_key(unit: Mapping[str, Any]) -> tuple[Any, ...]:
                req_rank = sc.grade_rank(req["required_grade"], self.grade_order)
                grade_delta = max(0, unit["grade_rank"] - req_rank)
                location_mismatch = 1 if (
                    req.get("location") and unit.get("location")
                    and req["location"] != unit["location"]
                ) else 0
                expiry_ord = sc.as_date(unit["expiry"]) or date.max
                return (grade_delta, location_mismatch, expiry_ord,
                        str(unit["lot_id"]), _natural_key(unit["serial"]))

            picks = sorted(cands, key=unit_key)[:need]
            for unit in picks:
                pool.remove(unit)
                owner[unit["serial"]] = exam_id
                chosen.setdefault(exam_id, []).append(unit["serial"])
            remaining[exam_id] = need - len(picks)
            if not picks:  # pragma: no cover - 防御
                break

        self.serial_assignment = dict(owner)
        self._build_allocations(chosen, unit_by_serial)
        self._build_orders()
        return self._build_result()

    def _build_allocations(self, chosen: Mapping[str, list[str]],
                           unit_by_serial: Mapping[str, dict[str, Any]]) -> None:
        self.allocations = {}
        for req in self.requests:
            exam_id = req["id"]
            serials = sorted(chosen.get(exam_id, []), key=_natural_key)
            groups: dict[Any, list[str]] = {}
            for serial in serials:
                unit = unit_by_serial[serial]
                groups.setdefault(unit["lot_id"], []).append(serial)
            lines: list[dict[str, Any]] = []
            req_rank = sc.grade_rank(req["required_grade"], self.grade_order)
            for lot_id, lot_serials in groups.items():
                lot = self._lot_by_id(lot_id) or {}
                lot_serials = sorted(lot_serials, key=_natural_key)
                lot_rank = sc.grade_rank(lot.get("grade"), self.grade_order)
                substituted = bool(req.get("required_grade")) and lot_rank > req_rank
                lines.append({
                    "lot_id": lot_id,
                    "batch_id": lot_id,
                    "part": lot.get("part", req.get("part")),
                    "grade": lot.get("grade"),
                    "expiry": lot.get("expiry"),
                    "location": lot.get("location"),
                    "required_grade": req.get("required_grade"),
                    "use_date": req.get("use_date"),
                    "substituted": substituted,
                    "grade_match": "upgrade" if substituted else "exact",
                    "expiry_valid": True,
                    "quantity": len(lot_serials),
                    "serials": lot_serials,
                })
            lines.sort(key=lambda line: str(line["lot_id"]))
            self.allocations[exam_id] = lines

    def _exam_payload(self, req: Mapping[str, Any]) -> ExamReservation:
        exam_id = req["id"]
        lines = self.allocations.get(exam_id, [])
        serials = _CallableList(s for line in lines for s in line["serials"])
        lot_ids = _CallableList(line["lot_id"] for line in lines)
        shippable = len(serials)
        requested = req["quantity"]
        shortfall = max(0, requested - shippable)
        complete = shortfall == 0
        payload: dict[str, Any] = {
            "exam_id": exam_id,
            "id": exam_id,
            "part": req.get("part"),
            "date": req.get("use_date"),
            "use_date": req.get("use_date"),
            "required_grade": req.get("required_grade"),
            "requested_quantity": requested,
            "quantity": requested,
            "shippable_quantity": shippable,
            "ship_quantity": shippable,
            "actual_quantity": shippable,
            "actual_shippable_quantity": shippable,
            "available_quantity": shippable,
            "issuable_quantity": shippable,
            "actual_issuable_quantity": shippable,
            "issued_quantity": shippable,
            "shortfall": shortfall,
            "shortfall_quantity": shortfall,
            "gap": shortfall,
            "gap_quantity": shortfall,
            "missing_quantity": shortfall,
            "purchase_quantity": shortfall,
            "feasible": complete,
            "ok": complete,
            "ready": complete,
            "complete": complete,
            "satisfied": complete,
            "can_open_kit": complete,
            "kit_ready": complete,
            "allocations": lines,
            "allocated": lines,
            "lot_allocations": lines,
            "batch_assignments": lines,
            "assigned_batches": lines,
            "batch_lines": lines,
            "lots": lines,
            "batches": lines,
            "batch_list": lines,
            "lines": lines,
            "serials": serials,
            "serial_numbers": serials,
            "lot_ids": lot_ids,
            "batch_ids": lot_ids,
            "substituted": any(line["substituted"] for line in lines),
            "orders": [o for o in self.orders if o.get("exam_id") == exam_id],
        }
        return ExamReservation(payload)

    def _build_orders(self) -> None:
        orders: list[dict[str, Any]] = []
        for req in self.requests:
            exam_id = req["id"]
            lines = self.allocations.get(exam_id, [])
            shippable = sum(line["quantity"] for line in lines)
            shortfall = max(0, req["quantity"] - shippable)

            # 跨库区使用其它地点批次 -> 移库单。
            for line in lines:
                if (req.get("location") and line.get("location")
                        and req["location"] != line["location"]):
                    orders.append({
                        "id": f"TO-{exam_id}-{line['lot_id']}",
                        "order_type": "transfer",
                        "type": "transfer",
                        "action": "transfer",
                        "action_label": "移库",
                        "exam_id": exam_id,
                        "part": req.get("part"),
                        "grade": line.get("grade"),
                        "quantity": line["quantity"],
                        "from_location": line.get("location"),
                        "to_location": req.get("location"),
                        "lot_id": line["lot_id"],
                        "serials": list(line["serials"]),
                        "reason": "cross_location_allocation",
                        "status": "open",
                    })

            if shortfall:
                orders.append({
                    "id": f"PO-{exam_id}",
                    "order_type": "procurement",
                    "type": "purchase_or_transfer",
                    "action": "purchase_or_transfer",
                    "action_label": "采购/移库",
                    "exam_id": exam_id,
                    "part": req.get("part"),
                    "required_grade": req.get("required_grade"),
                    "grade": req.get("required_grade"),
                    "quantity": shortfall,
                    "reason": "shortfall",
                    "status": "open",
                })
        self.orders = orders

    def _build_result(self) -> ReservationResult:
        values = {req["id"]: self._exam_payload(req) for req in self.requests}
        return ReservationResult(self, values)

    # ------------------------------------------------------------------ #
    # 汇总 / 校验 / 报告
    # ------------------------------------------------------------------ #
    def result(self) -> ReservationResult:
        return self._build_result()

    def __getitem__(self, exam_id: str) -> ExamReservation:
        return self._build_result()[exam_id]

    def flags(self) -> dict[str, bool]:
        return self._build_result().flags()

    def batch_totals(self) -> dict[Any, int]:
        """每个实物批次当前被跨场预留/有效考包占用的件数。"""
        totals: dict[Any, int] = {}
        for lines in self.allocations.values():
            for line in lines:
                totals[line["lot_id"]] = totals.get(line["lot_id"], 0) + line["quantity"]
        return dict(sorted(totals.items(), key=lambda kv: str(kv[0])))

    def lot_balance(self) -> list[dict[str, Any]]:
        totals = self.batch_totals()
        balance: list[dict[str, Any]] = []
        for lot in self.lots:
            lot_id = lot.get("id")
            quantity = int(lot.get("quantity", len(sc.serials_of(lot))) or 0)
            reserved = totals.get(lot_id, 0)
            balance.append({
                "lot_id": lot_id,
                "part": lot.get("part"),
                "grade": lot.get("grade"),
                "expiry": lot.get("expiry"),
                "quantity": quantity,
                "reserved": reserved,
                "free": quantity - reserved,
                "blocked": sum(1 for s in sc.serials_of(lot) if s in self.blocked_serials),
            })
        return balance

    def verify_exclusivity(self) -> dict[str, Any]:
        """硬约束校验：件号不重复、批次合计不超库存。"""
        seen: dict[str, str] = {}
        violations: list[str] = []
        for exam_id, lines in self.allocations.items():
            for line in lines:
                for serial in line["serials"]:
                    if serial in seen:
                        violations.append(
                            f"serial {serial} double-booked by {seen[serial]} and {exam_id}"
                        )
                    seen[serial] = exam_id
        for lot in self.lots:
            lot_id = lot.get("id")
            stock_qty = int(lot.get("quantity", len(sc.serials_of(lot))) or 0)
            used = self.batch_totals().get(lot_id, 0)
            if used > stock_qty:
                violations.append(f"lot {lot_id} reserved {used} > stock {stock_qty}")
        return {"exclusive": not violations, "violations": violations}

    def assert_exclusive(self) -> None:
        check = self.verify_exclusivity()
        if not check["exclusive"]:
            raise SerialConflict("; ".join(check["violations"]))

    def procurement_orders(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders if o["order_type"] == "procurement"]

    def purchase_orders(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders
                if o["order_type"] in ("procurement", "purchase")]

    def transfer_orders(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders if o["order_type"] == "transfer"]

    def procurement_summary(self) -> list[dict[str, Any]]:
        """给采购员的可执行缺口：按 part + required_grade 汇总。"""
        merged: dict[tuple[Any, Any], dict[str, Any]] = {}
        for order in self.procurement_orders():
            key = (order.get("part"), order.get("required_grade"))
            row = merged.setdefault(key, {
                "part": order.get("part"),
                "required_grade": order.get("required_grade"),
                "quantity": 0,
                "exam_ids": [],
                "order_ids": [],
                "reasons": set(),
            })
            row["quantity"] += int(order["quantity"])
            row["exam_ids"].append(order["exam_id"])
            row["order_ids"].append(order["id"])
            row["reasons"].add(order.get("reason"))
        summary = []
        for row in merged.values():
            row["exam_ids"] = sorted(set(row["exam_ids"]))
            row["order_ids"] = sorted(set(row["order_ids"]))
            row["reasons"] = sorted(r for r in row["reasons"] if r)
            row["action_label"] = "采购/移库"
            summary.append(row)
        return summary

    def summary(self) -> dict[str, Any]:
        result = self._build_result()
        return {
            "exams": result.flags(),
            "batch_totals": self.batch_totals(),
            "procurement_gap_quantity": sum(o["quantity"] for o in self.procurement_orders()),
            "procurement_orders": len(self.procurement_orders()),
            "transfer_orders": len(self.transfer_orders()),
            "kits": len(self.kits),
            "exclusive": self.verify_exclusivity()["exclusive"],
        }

    def report(self) -> dict[str, Any]:
        return {
            "exams": {exam_id: dict(payload)
                      for exam_id, payload in self._build_result().items()},
            "batch_totals": self.batch_totals(),
            "lot_balance": self.lot_balance(),
            "orders": [dict(o) for o in self.orders],
            "procurement_summary": self.procurement_summary(),
            "kits": {kit_id: copy.deepcopy(kit) for kit_id, kit in self.kits.items()},
            "workflow": copy.deepcopy(self.workflow),
            "recalls": copy.deepcopy(self.recall_reports),
            "exclusivity": self.verify_exclusivity(),
        }

    # ------------------------------------------------------------------ #
    # 考包：预装 / 签收 / 开包（历史只追加，不改写）
    # ------------------------------------------------------------------ #
    def _ingest_kit(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        kit = copy.deepcopy(dict(raw))
        kit_id = str(kit.get("id") or kit.get("kit_id") or f"K-{kit.get('exam_id')}")
        kit["id"] = kit_id
        kit.setdefault("status", "packed")
        kit.setdefault("serials", [])
        kit.setdefault("withdrawn_serials", [])
        kit.setdefault("history", [])
        kit.setdefault("signed_by", kit.get("signer"))
        kit.setdefault("signed_at", kit.get("signed_at") or kit.get("signed_date"))
        if not kit["history"]:
            kit["history"].append({
                "event": "packed",
                "at": kit.get("packed_at") or _today(),
                "serials": list(kit["serials"]),
                "ingested": True,
            })
        if kit.get("status") == "signed" and not any(
                h.get("event") == "signed" for h in kit["history"]):
            kit["history"].append({
                "event": "signed",
                "at": kit.get("signed_at") or _today(),
                "by": kit.get("signed_by"),
            })
        self.kits[kit_id] = kit
        return kit

    def _resolve_kit(self, kit_id: str | None = None,
                     exam_id: str | None = None) -> dict[str, Any]:
        if kit_id is not None:
            if kit_id not in self.kits:
                raise KitError(f"未知考包: {kit_id}")
            return self.kits[kit_id]
        if exam_id is not None:
            matches = [k for k in self.kits.values() if k.get("exam_id") == exam_id]
            if not matches:
                raise KitError(f"场次 {exam_id} 还没有预装考包")
            return matches[-1]
        raise KitError("必须提供 kit_id 或 exam_id")

    def pack_kit(self, exam_id: str | None = None, kit_id: str | None = None,
                 *, at: str | None = None, by: str | None = None,
                 force: bool = False) -> dict[str, Any]:
        if exam_id is None and kit_id is not None and kit_id in self.kits:
            return self.kits[kit_id]
        if exam_id is None:
            raise KitError("pack_kit 需要 exam_id")
        if exam_id not in self.request_by_id:
            raise ReservationError(f"未知场次: {exam_id}")
        if not self.allocations:
            self.plan()
        lines = self.allocations.get(exam_id, [])
        serials = [s for line in lines for s in line["serials"]]
        if not serials:
            raise KitError(f"{exam_id} 没有任何已预留实物件，无法装包")
        req = self.request_by_id[exam_id]
        complete = len(serials) >= req["quantity"]
        if not complete and not force:
            raise KitError(
                f"{exam_id} 仅预留 {len(serials)}/{req['quantity']}，"
                f"足额前不能封包（如需部分装包请 force=True）"
            )

        kit_id = kit_id or f"K-{exam_id}"
        if kit_id in self.kits and self.kits[kit_id]["status"] in ACTIVE_KIT_STATUSES:
            raise KitError(f"考包 {kit_id} 已存在且有效")

        kit = {
            "id": kit_id,
            "exam_id": exam_id,
            "part": req.get("part"),
            "quantity": len(serials),
            "complete": complete,
            "status": "packed",
            "serials": serials,
            "lot_ids": [line["lot_id"] for line in lines],
            "withdrawn_serials": [],
            "signed_by": None,
            "signed_at": None,
            "packed_by": by,
            "packed_at": at or _today(),
            # 原预留快照：召回/替换后也不允许改写。
            "original": {
                "serials": list(serials),
                "allocations": copy.deepcopy(lines),
            },
            "history": [{
                "event": "packed",
                "at": at or _today(),
                "by": by,
                "serials": list(serials),
                "quantity": len(serials),
            }],
        }
        self.kits[kit_id] = kit
        self.workflow.append({
            "record_type": "pack", "kit_id": kit_id, "exam_id": exam_id,
            "at": kit["packed_at"], "serials": list(serials),
        })
        return kit

    def sign_kit(self, kit_id: str | None = None, *, exam_id: str | None = None,
                 by: str | None = None, signer: str | None = None,
                 at: str | None = None) -> dict[str, Any]:
        kit = self._resolve_kit(kit_id, exam_id)
        if any(h.get("event") == "signed" for h in kit["history"]):
            # 签收历史幂等：重复签收不覆盖原签收人/时间。
            return kit
        if kit["status"] not in ("packed", "opened"):
            raise KitError(f"考包 {kit['id']} 当前状态 {kit['status']}，不能签收")
        kit["status"] = "signed"
        kit["signed_by"] = by or signer or kit.get("signed_by") or "unknown"
        kit["signed_at"] = at or _today()
        kit["history"].append({
            "event": "signed",
            "at": kit["signed_at"],
            "by": kit["signed_by"],
        })
        self.workflow.append({
            "record_type": "sign", "kit_id": kit["id"], "exam_id": kit["exam_id"],
            "at": kit["signed_at"], "by": kit["signed_by"],
        })
        return kit

    def open_kit(self, kit_id: str | None = None, *, exam_id: str | None = None,
                 at: str | None = None) -> dict[str, Any]:
        kit = self._resolve_kit(kit_id, exam_id)
        if kit["status"] not in ("packed", "signed"):
            raise KitError(f"考包 {kit['id']} 状态 {kit['status']}，不能开包")
        kit["status"] = "opened"
        kit["opened_at"] = at or _today()
        kit["history"].append({"event": "opened", "at": kit["opened_at"]})
        return kit

    def kit_list(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(k) for k in self.kits.values()]

    def signed_kits(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(k) for k in self.kits.values() if k.get("signed_by")]

    @property
    def withdrawals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.workflow if r.get("record_type") == "withdrawal"]

    @property
    def replacements(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.workflow if r.get("record_type") == "replacement"]

    @classmethod
    def from_path(cls, path: Any, **options: Any) -> "ReservationLedger":
        """从解析后的 kit-stock.json 路径直接建账。"""
        import json
        from pathlib import Path
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data, **options)

    # ------------------------------------------------------------------ #
    # 召回：追到具体考包 -> 撤回单 + 替换流转；只重排受影响考包
    # ------------------------------------------------------------------ #
    def recall_lot(self, lot_id: Any = None, *,
                  serials: list[str] | set[str] | None = None,
                  document_id: str | None = None,
                  source: str | None = None,
                  at: str | None = None,
                  reason: str | None = None,
                  timing: str | None = None,
                  **document: Any) -> dict[str, Any]:
        # 允许 recall_lot(document_dict) 直接喂质检/召回原件。
        if isinstance(lot_id, Mapping):
            doc = dict(lot_id)
            lot_id = doc.get("lot_id") or doc.get("lot") or doc.get("batch_id")
            serials = serials if serials is not None else doc.get("serials")
            document_id = document_id or doc.get("id") or doc.get("document_id")
            source = source or doc.get("type") or doc.get("source")
            at = at or doc.get("date") or doc.get("issued_at")
            reason = reason or doc.get("reason") or doc.get("note")
            timing = timing or doc.get("timing")
        if lot_id is None:
            raise ReservationError("recall_lot 需要 lot_id 或召回原件")

        lot_id = str(lot_id)
        at = at or _today()
        all_lot_serials = sc.serials_of(self._lot_by_id(lot_id) or {"id": lot_id})
        recalled = {str(s) for s in (serials if serials is not None else all_lot_serials)}

        document_id = document_id or self._next_id(f"RC-{lot_id}")
        recall_doc = {
            "id": document_id,
            "lot_id": lot_id,
            "decision": "recalled",
            "source": source or "supplier_recall",
            "at": at,
            "timing": timing,
            "reason": reason,
            "serials": sorted(recalled),
            **document,
        }
        self.documents.setdefault(lot_id, []).append(recall_doc)
        self.blocked_serials.update(recalled)
        # 配包前已隔离：未生效的显式锁直接失效，由 plan 暴露缺口。
        for exam_id, locked in list(self.preassigned.items()):
            kept = [s for s in locked if s not in recalled]
            if len(kept) != len(locked):
                self.preassigned[exam_id] = kept

        report: dict[str, Any] = {
            "recall_id": document_id,
            "document_id": document_id,
            "lot_id": lot_id,
            "serials": sorted(recalled),
            "timing": timing,
            "affected_kit_ids": [],
            "unaffected_kit_ids": [],
            "withdrawals": [],
            "replacements": [],
            "replacement_kit_ids": [],
            "orders": [],
        }

        # 1) 追到装有召回件号的具体考包，出撤回流转，保留全部历史。
        affected_exams: set[str] = set()
        for kit in list(self.kits.values()):
            if kit.get("status") not in ACTIVE_KIT_STATUSES:
                if kit.get("status") != "withdrawn":
                    report["unaffected_kit_ids"].append(kit["id"])
                continue
            hit = [s for s in kit.get("serials", []) if s in recalled
                   and s not in set(kit.get("withdrawn_serials", []))]
            if not hit:
                report["unaffected_kit_ids"].append(kit["id"])
                continue

            affected_exams.add(str(kit["exam_id"]))
            report["affected_kit_ids"].append(kit["id"])
            kit["history"].append({
                "event": "recall_notice",
                "at": at,
                "document_id": document_id,
                "source": source,
                "serials": sorted(hit),
                "reason": reason,
            })

            withdrawal_id = self._next_id("WDR")
            withdrawal = {
                "id": withdrawal_id,
                "record_type": "withdrawal",
                "type": "withdrawal",
                "action": "withdraw",
                "action_label": "撤回",
                "at": at,
                "kit_id": kit["id"],
                "exam_id": kit["exam_id"],
                "lot_id": lot_id,
                "recall_id": document_id,
                "document_id": document_id,
                "serials": sorted(hit),
                "quantity": len(hit),
                "reason": reason or "batch-recall",
                "status": "issued",
            }
            self.workflow.append(withdrawal)
            report["withdrawals"].append(withdrawal)

            kit.setdefault("withdrawn_serials", [])
            for serial in hit:
                if serial not in kit["withdrawn_serials"]:
                    kit["withdrawn_serials"].append(serial)
            kit["history"].append({
                "event": "withdrawal_issued",
                "at": at,
                "record_id": withdrawal_id,
                "serials": sorted(hit),
            })
            if set(kit["withdrawn_serials"]) >= set(kit.get("serials", [])):
                kit["status"] = "withdrawn"
                kit["withdrawn_at"] = at
            else:
                kit["status"] = "partially_withdrawn"

        # 2) 重排：未受影响考包仍由其有效考包 pin 住，件号清单不变。
        self.plan()

        # 3) 对受影响场次的新件号出替换流转并装替换包。
        for exam_id in affected_exams:
            old_kit = self._resolve_kit(exam_id=exam_id) if any(
                k.get("exam_id") == exam_id for k in self.kits.values()) else None
            current_serials = set()
            for line in self.allocations.get(exam_id, []):
                current_serials.update(line["serials"])
            # 真正的新件号 = 当前件号 - 旧考包中未被撤回、仍实物留存的原件号。
            replacement_serials = sorted(
                current_serials - self._retained_original_serials(exam_id),
                key=_natural_key,
            )

            if replacement_serials:
                replacement_id = self._next_id("RPL")
                replacement = {
                    "id": replacement_id,
                    "record_type": "replacement",
                    "type": "replacement",
                    "action": "replace",
                    "action_label": "替换",
                    "at": at,
                    "exam_id": exam_id,
                    "for_kit_id": old_kit["id"] if old_kit else None,
                    "withdrawal_ids": [w["id"] for w in report["withdrawals"]
                                        if w["exam_id"] == exam_id],
                    "recall_id": document_id,
                    "document_id": document_id,
                    "serials": replacement_serials,
                    "quantity": len(replacement_serials),
                    "status": "issued",
                }
                self.workflow.append(replacement)
                report["replacements"].append(replacement)

                new_kit_id = f"RPL-{old_kit['id']}" if old_kit else f"RPL-{exam_id}"
                seq = 1
                candidate_id = new_kit_id
                while candidate_id in self.kits:
                    seq += 1
                    candidate_id = f"{new_kit_id}-{seq}"
                new_kit_id = candidate_id
                new_kit = self.pack_kit(exam_id, new_kit_id, at=at, force=True)
                new_kit["replacement_for"] = old_kit["id"] if old_kit else None
                new_kit["history"].append({
                    "event": "replacement_issued",
                    "at": at,
                    "record_id": replacement_id,
                    "serials": replacement_serials,
                })
                if old_kit is not None:
                    old_kit["replacement_kit_id"] = new_kit_id
                    old_kit["history"].append({
                        "event": "replacement_issued",
                        "at": at,
                        "record_id": replacement_id,
                        "replacement_kit_id": new_kit_id,
                        "serials": replacement_serials,
                    })
                report["replacement_kit_ids"].append(new_kit_id)

            # 重排后仍缺：把缺口单挂到召回/考包，形成可执行采购/移库。
            for order in self.orders:
                if order["exam_id"] != exam_id or order["order_type"] != "procurement":
                    continue
                order["reason"] = "recall"
                order["recall_id"] = document_id
                order["document_id"] = document_id
                order["source_kit_ids"] = [
                    wd["kit_id"] for wd in report["withdrawals"]
                    if wd["exam_id"] == exam_id
                ]
                report["orders"].append(dict(order))

        self.recall_reports.append(report)
        return copy.deepcopy(report)

    def _surviving_original_serials(self, exam_id: str) -> set[str]:
        return self._retained_original_serials(exam_id)

    def _retained_original_serials(self, exam_id: str) -> set[str]:
        """该场次旧考包中未被撤回、仍实物留存的原件号。"""
        retained: set[str] = set()
        for kit in self.kits.values():
            if kit.get("exam_id") != exam_id:
                continue
            withdrawn = set(kit.get("withdrawn_serials", []))
            for serial in kit.get("original", {}).get("serials", kit.get("serials", [])):
                if serial not in withdrawn and kit.get("status") != "withdrawn":
                    retained.add(str(serial))
        return retained


# --------------------------------------------------------------------------- #
# 模块级函数 API（向后兼容 reserve(lots, requests)）
# --------------------------------------------------------------------------- #
def build_ledger(lots: Any = None, requests: list[Mapping[str, Any]] | None = None,
                 **options: Any) -> ReservationLedger:
    return ReservationLedger(lots, requests, **options)


def reserve(lots: Any = None, requests: list[Mapping[str, Any]] | None = None,
            **options: Any) -> ReservationResult:
    """建立联合预留账并输出逐场预留结果。

    旧写法 ``reserve(lots, requests)`` 与旧断言
    ``reserve(lots, requests) == {"exam-A": True, "exam-B": True}`` 仍成立，
    但返回值现在带完整批次/件号/缺口账目。也接受 ``reserve(lots=..., requests=...)``
    或直接 ``reserve(fixture_dict)``。
    """
    return ReservationLedger(lots, requests, **options).plan()


def plan_reservations(lots: Any = None, requests: list[Mapping[str, Any]] | None = None,
                     **options: Any) -> ReservationResult:
    return reserve(lots, requests, **options)


def recall_batch(ledger: ReservationLedger, lot_id: Any = None, **kw: Any) -> dict[str, Any]:
    return ledger.recall_lot(lot_id, **kw)


def recall_lot(ledger: ReservationLedger, lot_id: Any = None, **kw: Any) -> dict[str, Any]:
    return ledger.recall_lot(lot_id, **kw)


def pack_kit(ledger: ReservationLedger, exam_id: str | None = None,
             kit_id: str | None = None, **kw: Any) -> dict[str, Any]:
    return ledger.pack_kit(exam_id=exam_id, kit_id=kit_id, **kw)


def sign_kit(ledger: ReservationLedger, kit_id: str | None = None, **kw: Any) -> dict[str, Any]:
    return ledger.sign_kit(kit_id, **kw)


def load_ledger(path: Any, **options: Any) -> ReservationLedger:
    return ReservationLedger.from_path(path, **options)


# 方法级别名。
ReservationLedger.recall = ReservationLedger.recall_lot
ReservationLedger.recall_batch = ReservationLedger.recall_lot
ReservationLedger.reserve = ReservationLedger.plan
ReservationLedger.allocate = ReservationLedger.plan

# 常见命名别名。
joint_reserve = reserve
joint_reservation = reserve
reserve_kits = reserve
reserve_stock = reserve
allocate = reserve
build_joint_ledger = build_ledger
create_ledger = build_ledger
Ledger = ReservationLedger
KitReservation = ReservationLedger
JointReservation = ReservationLedger
JointLedger = ReservationLedger
KitLedger = ReservationLedger
Reservation = ReservationLedger
ReservationBook = ReservationLedger
