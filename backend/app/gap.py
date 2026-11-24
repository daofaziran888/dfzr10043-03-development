"""采集差额视图：按实际采集记录关联采集、交接、隔离与迟到回执各阶段。

流程执行列表只给出「产出记录数」，无法回答“交接少了的几行到底在哪”。
本模块把一次执行的实际采集记录逐行关联到各阶段，并沉淀不可变的差额快照：

* 采集阶段：执行实际报出的记录行（`_gap_evidence`，按执行编号严格隔离）。
* 交接阶段：已有效交接的行；有效迟到回执同样计入，但保留迟到标记。
* 校验隔离：采集到了、但校验不通过被隔离的行，不进入补采范围。
* 缺失 / 待核：未交接且未隔离的行；确认缺失与待核分别输出。

差额快照把生成当时的采集证据、交接回执和确认结论整体冻结下来，迟到回执
到达后仍可用原采集证据复算旧差额；新证据或新回执会生成新的差额版本。补采
请求按（执行编号、差额编号、来源编号）幂等，同时提交只落一笔、兑现也只成功
一次。排程的漏采核对记录直接读取差额里的执行编号和缺失来源范围，派发实际
补采时再按当前回执过滤，已由迟报补齐的来源不会被重新归集。

内部数据表统一以 `_gap_` 前缀放在 store 中，`Store.module_names()` 会过滤掉这
类内部表，因此不会改变现有业务模块的列表与运营概览。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.dedup import sha256_hex
from app.store import store

EVIDENCE_TABLE = "_gap_evidence"
RECEIPT_TABLE = "_gap_receipt"
MISSING_TABLE = "_gap_missing"
GAP_TABLE = "_gap_gap"
CLAIM_TABLE = "_gap_claim"
REQUEST_TABLE = "_gap_request"

STATE_RECEIVED = "已交接"
STATE_LATE = "已交接（迟报）"
STATE_ISOLATED = "校验隔离"
STATE_REPLENISHED = "已补采"
STATE_CONFIRMED = "确认缺失"
STATE_PENDING = "待核"

RECEIVED_STATES = {STATE_RECEIVED, STATE_LATE, STATE_REPLENISHED}
MISSING_STATES = {STATE_CONFIRMED, STATE_PENDING}
VALID_RECEIPT_STATES = {"已接收", "有效迟报"}
VALID_EVIDENCE_STATES = {"校验通过", STATE_ISOLATED}
OPEN_CLAIM_STATES = {"待取数", "补采中"}
CLAIM_WINDOW_MINUTES = 30

# 同步路由跑在线程池里；同一进程内的重复领用、同时补采、重复兑现都靠这把锁串行化。
_LOCK = threading.RLock()


class GapError(ValueError):
    """差额视图的领域校验错误，路由层转成 400。"""


# ── 通用工具 ──────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _execution_code(value: Any) -> str:
    code = str(value or "").strip()
    if not code:
        raise GapError("执行编号不能为空")
    return code


def _row_no(row: dict[str, Any]) -> int:
    try:
        return int(str(row.get("行号", "0")))
    except (TypeError, ValueError):
        return 0


def _source_of(row: dict[str, Any]) -> str:
    return str(row.get("来源编号", ""))


def _next_id(table: str) -> int:
    rows = store.rows(table)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_code(table: str, prefix: str) -> str:
    return f"{prefix}-{_next_id(table):04d}"


def _execution_row(execution_code: str) -> dict[str, Any] | None:
    for row in store.rows("flow_run"):
        if row.get("执行编号") == execution_code:
            return row
    return None


def _sort_by_row(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (_row_no(row), _source_of(row)))


# ── 采集证据 ──────────────────────────────────────────────────

def record_capture(
    execution_code: str,
    records: list[dict[str, Any]],
    *,
    batch_code: str | None = None,
    captured_at: str | None = None,
    source_type: str = "原始采集",
) -> dict[str, Any]:
    """登记一次执行实际报出的采集记录，按来源逐行落证据。

    每个来源在一次登记里只能出现一次；再次登记会形成更高的证据版本，
    旧版本仍保留给历史差额复算。执行编号是证据的第一隔离维度：同号来源
    出现在其他执行里不会互相覆盖。
    """
    execution_code = _execution_code(execution_code)
    if not records:
        raise GapError("采集记录不能为空")
    if source_type not in {"原始采集", "补采"}:
        raise GapError(f"未知采集类型：{source_type}")
    captured_at = captured_at or _now_iso()

    with _LOCK:
        existing = [
            row for row in store.rows(EVIDENCE_TABLE)
            if row.get("执行编号") == execution_code
        ]
        version = max((int(row.get("证据版本", 0)) for row in existing), default=0) + 1
        seen: set[str] = set()
        created: list[dict[str, Any]] = []
        for record in records:
            source = str(record.get("来源编号") or record.get("source") or "").strip()
            if not source:
                raise GapError("采集记录缺少来源编号")
            if source in seen:
                raise GapError(f"同一批采集记录中来源重复：{source}")
            seen.add(source)
            try:
                row_no = int(record.get("行号") or record.get("row_no") or 0)
            except (TypeError, ValueError) as exc:
                raise GapError(f"来源{source}的行号不是整数") from exc
            if row_no <= 0:
                raise GapError(f"来源{source}缺少有效行号")

            validation = str(record.get("校验状态") or "校验通过").strip()
            if validation in {"隔离", "isolated", "isolated_by_validation"}:
                validation = STATE_ISOLATED
            if validation not in VALID_EVIDENCE_STATES:
                raise GapError(f"来源{source}的校验状态不支持：{validation}")

            payload = record.get("证据内容", record.get("content", ""))
            if not isinstance(payload, str):
                payload = _dumps(payload)
            digest = str(record.get("证据摘要") or "").strip() or sha256_hex(
                f"{execution_code}|{source}|{row_no}|{payload}"
            )
            row = {
                "id": _next_id(EVIDENCE_TABLE),
                "执行编号": execution_code,
                "来源编号": source,
                "行号": row_no,
                "校验状态": validation,
                "来源类型": source_type,
                "证据摘要": digest,
                "证据内容": payload,
                "采集批次": batch_code or "",
                "采集时间": str(record.get("采集时间") or captured_at),
                "证据版本": version,
                "入库时间": _now_iso(),
            }
            store.rows(EVIDENCE_TABLE).append(row)
            created.append(row)

        return {
            "ok": True,
            "执行编号": execution_code,
            "证据版本": version,
            "记录数": len(created),
            "采集批次": batch_code or "",
            "来源范围": [_source_of(row) for row in created],
        }


def _latest_evidence(execution_code: str) -> dict[str, dict[str, Any]]:
    """取一次执行里每个来源的最新证据；其他执行的同号记录不会进入结果。"""
    latest: dict[str, dict[str, Any]] = {}
    for row in store.rows(EVIDENCE_TABLE):
        if row.get("执行编号") != execution_code:
            continue
        source = _source_of(row)
        current = latest.get(source)
        if current is None or (
            int(row.get("证据版本", 0)), int(row.get("id", 0))
        ) > (
            int(current.get("证据版本", 0)), int(current.get("id", 0))
        ):
            latest[source] = row
    return latest


# ── 交接回执 ──────────────────────────────────────────────────

def record_receipts(
    execution_code: str,
    sources: list[Any],
    *,
    handoff_code: str = "",
    late: bool = False,
    received_at: str | None = None,
) -> dict[str, Any]:
    """登记交接接收回执。`late=True` 表示迟到的有效回执。

    回执按来源 upsert，同一来源不会因为重复登记而多算一次交接；迟到回执
    覆盖为「有效迟报」。校验隔离来源不接受回执。回执到达后，同一来源的
    未取数领用会就地转为「已补齐」，挂起的补采请求撤销，不再重复取数。
    """
    execution_code = _execution_code(execution_code)
    if not sources:
        raise GapError("交接回执来源不能为空")
    received_at = received_at or _now_iso()

    normalized: list[tuple[str, bool]] = []
    for item in sources:
        if isinstance(item, dict):
            source = str(item.get("来源编号") or item.get("source") or "").strip()
            row_late = bool(item.get("迟到", late))
        else:
            source = str(item).strip()
            row_late = late
        if not source:
            raise GapError("交接回执缺少来源编号")
        normalized.append((source, row_late))

    with _LOCK:
        evidence = _latest_evidence(execution_code)
        unknown = [source for source, _ in normalized if source not in evidence]
        if unknown:
            raise GapError(f"来源不在该执行的采集证据中：{'、'.join(unknown)}")
        isolated = [
            source for source, _ in normalized
            if evidence[source].get("校验状态") == STATE_ISOLATED
        ]
        if isolated:
            raise GapError(f"校验隔离来源不能登记交接回执：{'、'.join(isolated)}")

        existing = {
            _source_of(row): row
            for row in store.rows(RECEIPT_TABLE)
            if row.get("执行编号") == execution_code
        }
        touched: list[dict[str, Any]] = []
        for source, row_late in normalized:
            status = "有效迟报" if row_late else "已接收"
            receipt = existing.get(source)
            if receipt is None:
                receipt = {
                    "id": _next_id(RECEIPT_TABLE),
                    "执行编号": execution_code,
                    "来源编号": source,
                    "交接编号": handoff_code,
                    "接收状态": status,
                    "迟到": row_late,
                    "接收时间": received_at,
                    "入库时间": _now_iso(),
                }
                store.rows(RECEIPT_TABLE).append(receipt)
                existing[source] = receipt
            else:
                receipt["接收状态"] = status
                receipt["迟到"] = bool(receipt.get("迟到")) or row_late
                receipt["交接编号"] = handoff_code or receipt.get("交接编号", "")
                receipt["最近回执时间"] = received_at
            touched.append(dict(receipt))

        settled = _settle_open_work(execution_code)
        return {
            "ok": True,
            "执行编号": execution_code,
            "交接编号": handoff_code,
            "登记来源": [row["来源编号"] for row in touched],
            "迟到": late,
            "已补齐领用": settled["claims"],
            "已撤销补采请求": settled["requests"],
        }


def _receipt_map(execution_code: str) -> dict[str, dict[str, Any]]:
    return {
        _source_of(row): row
        for row in store.rows(RECEIPT_TABLE)
        if row.get("执行编号") == execution_code
        and row.get("接收状态") in VALID_RECEIPT_STATES
    }


def _receipt_snapshot(receipts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "来源编号": receipt.get("来源编号", ""),
            "接收状态": receipt.get("接收状态", ""),
            "迟到": bool(receipt.get("迟到")),
            "交接编号": receipt.get("交接编号", ""),
            "接收时间": receipt.get("接收时间", ""),
        }
        for receipt in sorted(receipts.values(), key=lambda row: _source_of(row))
    ]


def _settle_open_work(execution_code: str) -> dict[str, list[str]]:
    """迟到/正常回执到达后收口同一来源的旧领用和挂起补采请求。"""
    received = _receipt_map(execution_code)
    resolved_claims: list[str] = []
    cancelled_requests: list[str] = []
    for claim in store.rows(CLAIM_TABLE):
        if claim.get("执行编号") != execution_code:
            continue
        if claim.get("来源编号") in received and claim.get("领用状态") in OPEN_CLAIM_STATES:
            claim["领用状态"] = "已补齐"
            claim["补齐方式"] = "有效迟报" if received[claim["来源编号"]].get("迟到") else "正常回执"
            claim["最近更新"] = _now_iso()
            resolved_claims.append(claim.get("领用编号", ""))
    for request in store.rows(REQUEST_TABLE):
        if request.get("执行编号") != execution_code:
            continue
        if request.get("来源编号") in received and request.get("请求状态") == "待执行":
            request["请求状态"] = "已撤销"
            request["撤销原因"] = "有效迟报已补齐，不再重复归集"
            request["最近更新"] = _now_iso()
            cancelled_requests.append(request.get("请求编号", ""))
    return {"claims": resolved_claims, "requests": cancelled_requests}


# ── 差额计算 ──────────────────────────────────────────────────

def _confirmations(execution_code: str) -> set[str]:
    return {
        _source_of(row)
        for row in store.rows(MISSING_TABLE)
        if row.get("执行编号") == execution_code
    }


def _request_index(execution_code: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for request in store.rows(REQUEST_TABLE):
        if request.get("执行编号") != execution_code:
            continue
        latest[_source_of(request)] = request
    return latest


def _claim_index(execution_code: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for claim in store.rows(CLAIM_TABLE):
        if claim.get("执行编号") != execution_code:
            continue
        latest[_source_of(claim)] = claim
    return latest


def _compose_rows(
    execution_code: str,
    evidence: dict[str, dict[str, Any]],
    receipts: dict[str, dict[str, Any]],
    confirmed: set[str],
    requests: dict[str, dict[str, Any]] | None = None,
    claims: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把一条执行的实际采集记录逐行关联到交接、隔离、补采与核对结论。"""
    requests = requests or {}
    claims = claims or {}
    rows: list[dict[str, Any]] = []
    for source in sorted(evidence, key=lambda item: (_row_no(evidence[item]), item)):
        item = evidence[source]
        receipt = receipts.get(source)
        request = requests.get(source)
        claim = claims.get(source)

        if item.get("校验状态") == STATE_ISOLATED:
            stage = STATE_ISOLATED
            gap_state = STATE_ISOLATED
            replenishable = False
        elif receipt is not None:
            late = bool(receipt.get("迟到"))
            stage = STATE_LATE if late else STATE_RECEIVED
            gap_state = stage
            replenishable = False
        elif item.get("来源类型") == "补采":
            # 只有落到采集证据里的补采才算补齐；仅凭请求状态不会改变差额判定，
            # 这样旧差额复算不会被后来的请求/兑现污染。
            stage = STATE_REPLENISHED
            gap_state = STATE_REPLENISHED
            replenishable = False
        elif source in confirmed:
            stage = "未交接"
            gap_state = STATE_CONFIRMED
            replenishable = True
        else:
            stage = "未交接"
            gap_state = STATE_PENDING
            replenishable = True

        rows.append({
            "行号": _row_no(item),
            "来源编号": source,
            "采集阶段": {
                "采集时间": item.get("采集时间", ""),
                "采集批次": item.get("采集批次", ""),
                "证据摘要": item.get("证据摘要", ""),
                "来源类型": item.get("来源类型", ""),
                "证据版本": int(item.get("证据版本", 0)),
            },
            "校验状态": item.get("校验状态", ""),
            "交接阶段": {
                "接收状态": receipt.get("接收状态", "") if receipt else "",
                "迟到": bool(receipt.get("迟到")) if receipt else False,
                "交接编号": receipt.get("交接编号", "") if receipt else "",
                "接收时间": receipt.get("接收时间", "") if receipt else "",
            },
            "阶段": stage,
            "差额状态": gap_state,
            "是否可补采": replenishable,
            "领用编号": claim.get("领用编号", "") if claim else "",
            "领用状态": claim.get("领用状态", "") if claim else "",
            "补采请求": request.get("请求编号", "") if request else "",
            "补采请求状态": request.get("请求状态", "") if request else "",
        })
    return rows


def _summarize(rows: list[dict[str, Any]], execution_code: str) -> dict[str, Any]:
    def sources(state: str) -> list[str]:
        return [row["来源编号"] for row in rows if row["差额状态"] == state]

    received = [row for row in rows if row["差额状态"] in RECEIVED_STATES]
    confirmed = sources(STATE_CONFIRMED)
    pending = sources(STATE_PENDING)
    isolated = sources(STATE_ISOLATED)
    replenished = sources(STATE_REPLENISHED)
    collected = len(rows)
    handed_off = len(received)
    return {
        "执行编号": execution_code,
        "采集总数": collected,
        "交接总数": handed_off,
        "差额总数": collected - handed_off,
        "已确认差额": len(confirmed),
        "待核": len(pending),
        "隔离": len(isolated),
        "已补采": len(replenished),
        "已确认差额来源": confirmed,
        "待核来源": pending,
        "隔离来源": isolated,
        "已补采来源": replenished,
        "缺失来源范围": [
            row["来源编号"] for row in rows
            if row["差额状态"] in MISSING_STATES
        ],
        "可补采来源": [row["来源编号"] for row in rows if row["是否可补采"]],
        "迟到来源": [
            row["来源编号"] for row in rows
            if row.get("交接阶段", {}).get("迟到")
        ],
    }


def current_view(execution_code: str) -> dict[str, Any]:
    """当前视图：直接按最新证据与回执计算，不依赖任何差额快照。"""
    execution_code = _execution_code(execution_code)
    with _LOCK:
        evidence = _latest_evidence(execution_code)
        receipts = _receipt_map(execution_code)
        rows = _compose_rows(
            execution_code, evidence, receipts, _confirmations(execution_code),
            _request_index(execution_code), _claim_index(execution_code),
        )
        summary = _summarize(rows, execution_code)
        run = _execution_row(execution_code)
        return {
            "执行编号": execution_code,
            "执行产出记录数": run.get("产出记录数") if run else None,
            "证据来源数": len(evidence),
            "摘要": summary,
            "来源明细": rows,
            "差额版本": _list_gap_summaries(execution_code),
        }


# ── 差额快照 ──────────────────────────────────────────────────

def _evidence_snapshot(evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "来源编号": row.get("来源编号", ""),
            "行号": _row_no(row),
            "校验状态": row.get("校验状态", ""),
            "来源类型": row.get("来源类型", ""),
            "证据摘要": row.get("证据摘要", ""),
            "采集批次": row.get("采集批次", ""),
            "采集时间": row.get("采集时间", ""),
            "证据版本": int(row.get("证据版本", 0)),
        }
        for row in _sort_by_row(evidence.values())
    ]


def _snapshot_digest(
    evidence_rows: list[dict[str, Any]],
    receipt_rows: list[dict[str, Any]],
    confirmed: list[str],
) -> str:
    return sha256_hex(_dumps({
        "证据": evidence_rows,
        "回执": receipt_rows,
        "确认": confirmed,
    }))


def _list_gap_summaries(execution_code: str | None = None) -> list[dict[str, Any]]:
    gaps = [
        gap for gap in store.rows(GAP_TABLE)
        if execution_code is None or gap.get("执行编号") == execution_code
    ]
    gaps.sort(key=lambda gap: (int(gap.get("差额版本", 0)), int(gap.get("id", 0))))
    return [_gap_summary(gap) for gap in gaps]


def _gap_or_error(gap_code: str) -> dict[str, Any]:
    for gap in store.rows(GAP_TABLE):
        if gap.get("差额编号") == gap_code:
            return gap
    raise GapError(f"差额 {gap_code} 不存在")


def _gap_summary(gap: dict[str, Any]) -> dict[str, Any]:
    return {
        "差额编号": gap.get("差额编号", ""),
        "执行编号": gap.get("执行编号", ""),
        "证据版本": gap.get("证据版本", 0),
        "差额版本": gap.get("差额版本", 0),
        "生成原因": gap.get("生成原因", ""),
        "状态": gap.get("状态", ""),
        "采集总数": gap.get("采集总数", 0),
        "交接总数": gap.get("交接总数", 0),
        "差额总数": gap.get("差额总数", 0),
        "已确认差额": gap.get("已确认差额", 0),
        "待核": gap.get("待核", 0),
        "隔离": gap.get("隔离", 0),
        "已补采": gap.get("已补采", 0),
        "已确认差额来源": _loads(gap.get("已确认差额来源"), []),
        "待核来源": _loads(gap.get("待核来源"), []),
        "隔离来源": _loads(gap.get("隔离来源"), []),
        "已补采来源": _loads(gap.get("已补采来源"), []),
        "缺失来源范围": _loads(gap.get("缺失来源范围"), []),
        "可补采来源": _loads(gap.get("可补采来源"), []),
        "迟到来源": _loads(gap.get("迟到来源"), []),
        "快照摘要": gap.get("快照摘要", ""),
        "关联核对": _loads(gap.get("关联核对"), []),
        "补采结果": _loads(gap.get("补采结果"), []),
        "生成时间": gap.get("生成时间", ""),
        "最近更新": gap.get("最近更新", ""),
    }


def _gap_snapshot_evidence(gap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("来源编号", "")): {
            **row,
            "证据版本": int(row.get("证据版本", 0)),
        }
        for row in _loads(gap.get("证据快照"), [])
    }


def _gap_snapshot_receipts(gap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("来源编号", "")): row
        for row in _loads(gap.get("回执快照"), [])
    }


def _gap_snapshot_confirmed(gap: dict[str, Any]) -> set[str]:
    return {str(source) for source in _loads(gap.get("确认快照"), [])}


def _recompose_gap(gap: dict[str, Any]) -> list[dict[str, Any]]:
    """按冻结的证据、回执、确认结论重算明细；领用与请求只作为展示列附上。"""
    execution_code = gap.get("执行编号", "")
    rows = _compose_rows(
        execution_code,
        _gap_snapshot_evidence(gap),
        _gap_snapshot_receipts(gap),
        _gap_snapshot_confirmed(gap),
        _request_index(execution_code),
        _claim_index(execution_code),
    )
    summary = _summarize(rows, execution_code)
    gap.update({
        "采集总数": summary["采集总数"],
        "交接总数": summary["交接总数"],
        "差额总数": summary["差额总数"],
        "已确认差额": summary["已确认差额"],
        "待核": summary["待核"],
        "隔离": summary["隔离"],
        "已补采": summary["已补采"],
        "已确认差额来源": _dumps(summary["已确认差额来源"]),
        "待核来源": _dumps(summary["待核来源"]),
        "隔离来源": _dumps(summary["隔离来源"]),
        "已补采来源": _dumps(summary["已补采来源"]),
        "缺失来源范围": _dumps(summary["缺失来源范围"]),
        "可补采来源": _dumps(summary["可补采来源"]),
        "迟到来源": _dumps(summary["迟到来源"]),
        "来源明细": _dumps(rows),
        "状态": "已补齐" if not summary["缺失来源范围"] else "开放",
    })
    return rows


def create_gap(execution_code: str, *, reason: str = "手动生成") -> dict[str, Any]:
    """按当前证据与回执生成一版差额快照，后续迟到回执不会改写它。"""
    execution_code = _execution_code(execution_code)
    with _LOCK:
        evidence = _latest_evidence(execution_code)
        if not evidence:
            raise GapError(f"执行 {execution_code} 尚无采集证据，无法生成差额")
        receipts = _receipt_map(execution_code)
        confirmed = sorted(_confirmations(execution_code))
        evidence_rows = _evidence_snapshot(evidence)
        receipt_rows = _receipt_snapshot(receipts)
        version = len([
            gap for gap in store.rows(GAP_TABLE)
            if gap.get("执行编号") == execution_code
        ]) + 1
        # 新版本沿用同一执行已写入排程的漏采核对，迟到回执后能继续处理旧领用与旧核对；
        # 补采结果仍只回写发起请求的那一笔差额。
        prior_checks = [
            gap for gap in store.rows(GAP_TABLE)
            if gap.get("执行编号") == execution_code
        ]
        inherited: list[str] = []
        for prior in prior_checks:
            for code in _loads(prior.get("关联核对"), []):
                if code not in inherited:
                    inherited.append(code)
        gap = {
            "id": _next_id(GAP_TABLE),
            "差额编号": _next_code(GAP_TABLE, "GAP"),
            "执行编号": execution_code,
            "证据版本": max(int(row.get("证据版本", 0)) for row in evidence.values()),
            "差额版本": version,
            "生成原因": reason,
            "证据快照": _dumps(evidence_rows),
            "回执快照": _dumps(receipt_rows),
            "确认快照": _dumps(confirmed),
            "快照摘要": _snapshot_digest(evidence_rows, receipt_rows, confirmed),
            "关联核对": _dumps(inherited),
            "补采结果": _dumps([]),
            "生成时间": _now_iso(),
        }
        _recompose_gap(gap)
        store.rows(GAP_TABLE).append(gap)
        return get_gap(gap["差额编号"])


def regenerate_gap(execution_code: str, reason: str = "证据或回执更新后重算") -> dict[str, Any]:
    """按当前实时证据与回执生成新一版差额，用于和原差额对照。"""
    return create_gap(execution_code, reason=reason)


def get_gap(gap_code: str) -> dict[str, Any]:
    with _LOCK:
        gap = _gap_or_error(gap_code)
        _recompose_gap(gap)
        return {
            **_gap_summary(gap),
            "来源明细": _loads(gap.get("来源明细"), []),
            "领用记录": _list_claims(gap_code=gap_code),
            "补采请求": _list_requests(gap_code=gap_code),
        }


def recalc_gap(gap_code: str) -> dict[str, Any]:
    """用原采集证据复算旧差额，并给出与当前实时状态的差异。"""
    with _LOCK:
        gap = _gap_or_error(gap_code)
        rows = _recompose_gap(gap)
        summary = _summarize(rows, gap.get("执行编号", ""))
        current = current_view(gap.get("执行编号", ""))["摘要"]
        return {
            "差额编号": gap_code,
            "执行编号": gap.get("执行编号", ""),
            "快照摘要": gap.get("快照摘要", ""),
            "复算": summary,
            "复算来源明细": rows,
            "当前": current,
            "与原记录一致": (
                summary["采集总数"] == gap.get("采集总数")
                and summary["交接总数"] == gap.get("交接总数")
                and summary["差额总数"] == gap.get("差额总数")
            ),
            "当前差异": {
                "交接总数": current["交接总数"] - summary["交接总数"],
                "差额总数": current["差额总数"] - summary["差额总数"],
                "待核": current["待核"] - summary["待核"],
                "已确认差额": current["已确认差额"] - summary["已确认差额"],
                "隔离": current["隔离"] - summary["隔离"],
                "已补采": current["已补采"] - summary["已补采"],
            },
        }


def confirm_missing(gap_code: str, sources: list[str], operator: str = "system") -> dict[str, Any]:
    """把差额里的待核来源确认为缺失，作为补采范围的依据。

    缺失结论挂在执行编号上，后续版本的差额和排程核对都复用同一结论；
    已交接、已补采或校验隔离的来源不允许再被确认成缺失。
    """
    with _LOCK:
        gap = _gap_or_error(gap_code)
        _recompose_gap(gap)
        rows = {row["来源编号"]: row for row in _loads(gap.get("来源明细"), [])}
        unknown = [source for source in sources if source not in rows]
        if unknown:
            raise GapError(f"来源不在该差额中：{'、'.join(unknown)}")
        rejected = [
            source for source in sources
            if rows[source]["差额状态"] not in MISSING_STATES
        ]
        if rejected:
            raise GapError(f"来源已交接或已隔离，不能确认为缺失：{'、'.join(rejected)}")
        execution_code = gap.get("执行编号", "")
        for source in sources:
            existing = next(
                (
                    row for row in store.rows(MISSING_TABLE)
                    if row.get("执行编号") == execution_code
                    and row.get("来源编号") == source
                ),
                None,
            )
            if existing is None:
                store.rows(MISSING_TABLE).append({
                    "id": _next_id(MISSING_TABLE),
                    "执行编号": execution_code,
                    "来源编号": source,
                    "结论": STATE_CONFIRMED,
                    "操作者": operator,
                    "确认时间": _now_iso(),
                    "差额编号": gap_code,
                })
            else:
                existing.update({
                    "结论": STATE_CONFIRMED,
                    "操作者": operator,
                    "确认时间": _now_iso(),
                    "差额编号": gap_code,
                })
        confirmed = _gap_snapshot_confirmed(gap) | {str(source) for source in sources}
        gap["确认快照"] = _dumps(sorted(confirmed))
        _recompose_gap(gap)
        return get_gap(gap_code)


# ── 领用窗口 ──────────────────────────────────────────────────

def claim_missing(
    gap_code: str,
    sources: list[str] | None = None,
    operator: str = "值班员",
) -> dict[str, Any]:
    """在领用窗口里认领缺失来源；重复认领返回原领用，不产生第二笔。"""
    with _LOCK:
        gap = _gap_or_error(gap_code)
        _recompose_gap(gap)
        allowed = _loads(gap.get("可补采来源"), [])
        requested = [str(source) for source in (sources or allowed)]
        if not requested:
            raise GapError("该差额没有可领用的缺失来源")
        invalid = [source for source in requested if source not in allowed]
        if invalid:
            raise GapError(f"来源不在可补采范围：{'、'.join(invalid)}")

        execution_code = gap.get("执行编号", "")
        open_claims = {
            _source_of(row): row
            for row in store.rows(CLAIM_TABLE)
            if row.get("执行编号") == execution_code
            and row.get("领用状态") in OPEN_CLAIM_STATES
        }
        deadline = (_now() + timedelta(minutes=CLAIM_WINDOW_MINUTES)).isoformat(
            timespec="seconds")
        created: list[dict[str, Any]] = []
        duplicates: list[str] = []
        for source in requested:
            if source in open_claims:
                duplicates.append(open_claims[source].get("领用编号", ""))
                continue
            claim = {
                "id": _next_id(CLAIM_TABLE),
                "领用编号": _next_code(CLAIM_TABLE, "CLM"),
                "差额编号": gap_code,
                "执行编号": execution_code,
                "来源编号": source,
                "领用状态": "待取数",
                "领用人": operator,
                "领用时间": _now_iso(),
                "窗口截止": deadline,
                "关联请求": "",
                "补齐方式": "",
                "备注": "",
                "最近更新": _now_iso(),
            }
            store.rows(CLAIM_TABLE).append(claim)
            open_claims[source] = claim
            created.append(dict(claim))
        return {
            "ok": True,
            "差额编号": gap_code,
            "执行编号": execution_code,
            "新建领用": created,
            "重复领用": duplicates,
            "领用窗口": _list_claims(gap_code=gap_code),
        }


def _claim_status(claim: dict[str, Any]) -> dict[str, Any]:
    try:
        deadline = datetime.fromisoformat(str(claim.get("窗口截止", "")))
    except ValueError:
        deadline = _now()
    remaining = int((deadline - _now()).total_seconds())
    return {
        **claim,
        "剩余秒数": max(remaining, 0),
        "是否超窗": remaining < 0 and claim.get("领用状态") == "待取数",
    }


def _list_claims(
    gap_code: str | None = None,
    execution_code: str | None = None,
    open_only: bool = True,
) -> list[dict[str, Any]]:
    rows = []
    for claim in store.rows(CLAIM_TABLE):
        if gap_code and claim.get("差额编号") != gap_code:
            continue
        if execution_code and claim.get("执行编号") != execution_code:
            continue
        if open_only and claim.get("领用状态") not in OPEN_CLAIM_STATES:
            continue
        rows.append(_claim_status(claim))
    return sorted(rows, key=lambda row: row.get("id", 0))


def list_claim_window(
    gap_code: str | None = None,
    execution_code: str | None = None,
    include_closed: bool = False,
) -> dict[str, Any]:
    claims = _list_claims(gap_code, execution_code, open_only=not include_closed)
    return {
        "领用窗口": claims,
        "待取数": sum(1 for row in claims if row.get("领用状态") == "待取数"),
        "补采中": sum(1 for row in claims if row.get("领用状态") == "补采中"),
        "已补齐": sum(1 for row in claims if row.get("领用状态") == "已补齐"),
        "已兑现": sum(1 for row in claims if row.get("领用状态") == "已兑现"),
        "已超窗": sum(1 for row in claims if row.get("是否超窗")),
    }


# ── 补采请求与兑现 ────────────────────────────────────────────

def _list_requests(
    gap_code: str | None = None,
    execution_code: str | None = None,
) -> list[dict[str, Any]]:
    rows = [
        request for request in store.rows(REQUEST_TABLE)
        if (not gap_code or request.get("差额编号") == gap_code)
        and (not execution_code or request.get("执行编号") == execution_code)
    ]
    return sorted(rows, key=lambda row: row.get("id", 0))


def _request_for_source(
    gap: dict[str, Any],
    source: str,
    operator: str,
    *,
    check_code: str = "",
) -> tuple[dict[str, Any], bool]:
    """按（执行编号、来源编号）幂等创建补采请求。

    同一来源即使从不同差额版本重复提交，也只会有一笔在途或已兑现的请求，
    因此实际补采最多兑现一次。
    """
    for request in store.rows(REQUEST_TABLE):
        if (
            request.get("执行编号") == gap.get("执行编号")
            and request.get("来源编号") == source
            and request.get("请求状态") in {"待执行", "已兑现"}
        ):
            return request, False
    request = {
        "id": _next_id(REQUEST_TABLE),
        "请求编号": _next_code(REQUEST_TABLE, "RPL"),
        "差额编号": gap.get("差额编号", ""),
        "执行编号": gap.get("执行编号", ""),
        "来源编号": source,
        "请求状态": "待执行",
        "请求人": operator,
        "请求时间": _now_iso(),
        "关联核对": check_code,
        "兑现时间": "",
        "补采结果": "",
        "证据摘要": "",
        "撤销原因": "",
        "最近更新": _now_iso(),
    }
    store.rows(REQUEST_TABLE).append(request)
    return request, True


def request_replenish(
    gap_code: str,
    sources: list[str] | None = None,
    operator: str = "值班员",
    *,
    check_code: str = "",
) -> dict[str, Any]:
    """提交补采请求；同一来源同时提交只会落一笔，重复提交返回原请求。"""
    with _LOCK:
        gap = _gap_or_error(gap_code)
        _recompose_gap(gap)
        execution_code = gap.get("执行编号", "")
        receipts = _receipt_map(execution_code)
        evidence = _latest_evidence(execution_code)
        live = {
            row["来源编号"]: row
            for row in _compose_rows(
                execution_code, evidence, receipts, _confirmations(execution_code),
                _request_index(execution_code), _claim_index(execution_code),
            )
        }
        requested = [str(source) for source in (sources or _loads(gap.get("缺失来源范围"), []))]
        if not requested:
            raise GapError("该差额没有可补采的缺失来源")
        invalid = [
            source for source in requested
            if source not in live or not live[source]["是否可补采"]
        ]
        if invalid:
            raise GapError(f"来源已交接、已隔离或不在缺失范围：{'、'.join(invalid)}")

        created: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for source in requested:
            request, is_new = _request_for_source(
                gap, source, operator, check_code=check_code)
            if is_new:
                created.append(dict(request))
            else:
                duplicates.append(dict(request))
            _bind_claim(execution_code, source, request["请求编号"])
        return {
            "ok": True,
            "差额编号": gap_code,
            "执行编号": execution_code,
            "新建请求": created,
            "重复请求": duplicates,
            "只兑现一次": True,
            "补采请求": _list_requests(gap_code=gap_code),
        }


def _bind_claim(execution_code: str, source: str, request_code: str) -> None:
    for claim in store.rows(CLAIM_TABLE):
        if (
            claim.get("执行编号") == execution_code
            and claim.get("来源编号") == source
            and claim.get("领用状态") in OPEN_CLAIM_STATES
        ):
            claim["关联请求"] = request_code
            if claim.get("领用状态") == "待取数":
                claim["领用状态"] = "补采中"
            claim["最近更新"] = _now_iso()


def _request_or_error(request_code: str) -> dict[str, Any]:
    for request in store.rows(REQUEST_TABLE):
        if request.get("请求编号") == request_code:
            return request
    raise GapError(f"补采请求 {request_code} 不存在")


def complete_replenish(
    request_code: str,
    records: list[dict[str, Any]],
    operator: str = "补采执行器",
) -> dict[str, Any]:
    """兑现补采请求：写入补采证据并把结果关联回该笔差额；只成功一次。"""
    with _LOCK:
        request = _request_or_error(request_code)
        if request.get("请求状态") == "已兑现":
            return {
                "ok": True,
                "请求编号": request_code,
                "差额编号": request.get("差额编号", ""),
                "执行编号": request.get("执行编号", ""),
                "已经兑现": True,
                "兑现时间": request.get("兑现时间", ""),
                "补采结果": _loads(request.get("补采结果"), {}),
                "message": "补采请求已兑现，本次未重复写入",
            }
        if request.get("请求状态") == "已撤销":
            raise GapError(f"补采请求已撤销：{request.get('撤销原因', '')}")

        execution_code = request.get("执行编号", "")
        source = request.get("来源编号", "")
        receipts = _receipt_map(execution_code)
        if source in receipts:
            raise GapError(f"来源{source}已由有效回执补齐，不得重新归集")
        evidence = _latest_evidence(execution_code)
        if source not in evidence:
            raise GapError(f"来源{source}不在该执行的采集证据中")
        if evidence[source].get("校验状态") == STATE_ISOLATED:
            raise GapError(f"来源{source}处于校验隔离，不能补采")

        if not records:
            raise GapError("补采兑现必须提交实际采集记录")
        normalized_records: list[dict[str, Any]] = []
        for record in records:
            record_source = str(
                record.get("来源编号") or record.get("source") or "").strip()
            if record_source != source:
                raise GapError(
                    f"补采请求只允许来源{source}，收到来源{record_source or '空'}")
            item = dict(record)
            if not item.get("行号") and not item.get("row_no"):
                # 补采仍是同一行的数据，行号沿用原采集证据，避免脏行混入。
                item["行号"] = evidence[source].get("行号", 0)
            normalized_records.append(item)
        records = normalized_records
        capture = record_capture(
            execution_code,
            records,
            source_type="补采",
            batch_code=str(records[0].get("采集批次") or ""),
        )
        result = {
            "来源编号": source,
            "记录数": capture["记录数"],
            "证据版本": capture["证据版本"],
            "证据摘要": str(records[0].get("证据摘要") or ""),
            "采集时间": str(records[0].get("采集时间") or _now_iso()),
            "关联核对": request.get("关联核对", ""),
            "操作者": operator,
        }
        request["请求状态"] = "已兑现"
        request["兑现时间"] = _now_iso()
        request["补采结果"] = _dumps(result)
        request["证据摘要"] = result["证据摘要"]
        request["最近更新"] = _now_iso()

        for claim in store.rows(CLAIM_TABLE):
            if (
                claim.get("执行编号") == execution_code
                and claim.get("来源编号") == source
                and claim.get("领用状态") in OPEN_CLAIM_STATES
            ):
                claim["领用状态"] = "已兑现"
                claim["补齐方式"] = "补采"
                claim["最近更新"] = _now_iso()

        for gap in store.rows(GAP_TABLE):
            if gap.get("差额编号") != request.get("差额编号"):
                continue
            results = _loads(gap.get("补采结果"), [])
            results.append({"请求编号": request_code, **result})
            gap["补采结果"] = _dumps(results)
            gap["最近更新"] = _now_iso()

        _link_check_result(request.get("关联核对", ""), request_code)

        return {
            "ok": True,
            "请求编号": request_code,
            "差额编号": request.get("差额编号", ""),
            "执行编号": execution_code,
            "已经兑现": False,
            "兑现时间": request["兑现时间"],
            "补采结果": result,
            "当前视图": current_view(execution_code)["摘要"],
        }


def _link_check_result(check_code: str, request_code: str) -> None:
    if not check_code:
        return
    check = next(
        (row for row in store.rows("_shortfall_check")
         if row.get("核对编号") == check_code),
        None,
    )
    if check is None:
        return
    linked = [
        row for row in _list_requests()
        if row.get("关联核对") == check_code
    ]
    pending = [row for row in linked if row.get("请求状态") == "待执行"]
    check["补采请求"] = _dumps([row.get("请求编号", "") for row in linked])
    check["补采结果"] = _dumps([
        {
            "请求编号": row.get("请求编号", ""),
            "来源编号": row.get("来源编号", ""),
            "结果": row.get("请求状态", ""),
        }
        for row in linked
    ])
    check["核对状态"] = "补采中" if pending else ("已完成" if linked else "待补采")
    check["status"] = check["核对状态"]
    check["结果摘要"] = (
        f"补采请求{len(linked)}笔，"
        f"已兑现{sum(1 for row in linked if row.get('请求状态') == '已兑现')}笔，"
        f"已撤销{sum(1 for row in linked if row.get('请求状态') == '已撤销')}笔"
    )
    check["最近更新"] = _now_iso()
    check["pending"] = bool(pending)
    check["abnormal"] = False


# ── 排程漏采核对 ──────────────────────────────────────────────

def _check_row(check_code: str) -> dict[str, Any] | None:
    return next(
        (row for row in store.rows("_shortfall_check")
         if row.get("核对编号") == check_code),
        None,
    )


def create_schedule_check(
    gap_code: str,
    *,
    plan_code: str = "",
    operator: str = "值班员",
) -> dict[str, Any]:
    """把差额的核对结果写入排程的漏采核对记录，带上执行编号与缺失来源范围。"""
    from app.services import service_for

    with _LOCK:
        gap = _gap_or_error(gap_code)
        _recompose_gap(gap)
        missing = _loads(gap.get("缺失来源范围"), [])
        if not missing:
            raise GapError("该差额没有缺失来源，无需发起漏采核对")
        service = service_for("shortfall_check")
        if service is None:
            raise GapError("漏采核对服务未注册")
        check_code = _next_code("_shortfall_check", "MCHK")
        entry, missing_fields = service.create_entry({
            "核对编号": check_code,
            "关联计划": plan_code,
            "执行编号": gap.get("执行编号", ""),
            "差额编号": gap_code,
            "缺失来源范围": _dumps(missing),
            "覆盖来源数": str(len(missing)),
            "补采请求": _dumps([]),
            "补采结果": _dumps([]),
            "核对状态": "待补采",
            "核对人": operator,
            "核对时间": _now_iso(),
            "结果摘要": f"差额{gap_code}确认缺失/待核来源{len(missing)}个，等待补采",
        })
        if entry is None:
            raise GapError(f"漏采核对记录缺少必填字段：{'、'.join(missing_fields)}")
        linked = _loads(gap.get("关联核对"), [])
        linked.append(check_code)
        gap["关联核对"] = _dumps(linked)
        gap["最近更新"] = _now_iso()
        return {
            "ok": True,
            "核对编号": check_code,
            "差额编号": gap_code,
            "执行编号": gap.get("执行编号", ""),
            "缺失来源范围": missing,
            "核对": get_schedule_check(check_code),
        }


def dispatch_check(check_code: str, operator: str = "排程补采") -> dict[str, Any]:
    """排程按核对记录发起实际补采：按当前回执过滤，已补齐来源不再归集。"""
    with _LOCK:
        check = _check_row(check_code)
        if check is None:
            raise GapError(f"漏采核对记录 {check_code} 不存在")
        if check.get("核对状态") == "已关闭":
            raise GapError("漏采核对记录已关闭，不能再次发起补采")

        gap_code = check.get("差额编号", "")
        gap = _gap_or_error(gap_code)
        scope = [str(source) for source in _loads(check.get("缺失来源范围"), [])]

        execution_code = gap.get("执行编号", "")
        receipts = _receipt_map(execution_code)
        evidence = _latest_evidence(execution_code)
        live = {
            row["来源编号"]: row
            for row in _compose_rows(
                execution_code, evidence, receipts, _confirmations(execution_code),
                _request_index(execution_code), _claim_index(execution_code),
            )
        }
        requested: list[str] = []
        skipped: list[dict[str, str]] = []
        for source in scope:
            row = live.get(source)
            if row is None:
                skipped.append({"来源编号": source, "原因": "该执行已无此来源证据"})
            elif not row["是否可补采"]:
                skipped.append({
                    "来源编号": source,
                    "原因": f"已由{row['差额状态']}覆盖，不再重新归集",
                })
            else:
                requested.append(source)

        created: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for source in requested:
            request, is_new = _request_for_source(
                gap, source, operator, check_code=check_code)
            if is_new:
                created.append(dict(request))
            else:
                duplicates.append(dict(request))
            _bind_claim(execution_code, source, request["请求编号"])

        check["核对状态"] = "补采中" if requested else "已完成"
        check["status"] = check["核对状态"]
        check["补采请求"] = _dumps([
            row.get("请求编号", "") for row in created + duplicates
        ])
        check["结果摘要"] = (
            f"发起补采{len(requested)}个来源，跳过{len(skipped)}个已补齐来源"
        )
        check["最近更新"] = _now_iso()
        check["pending"] = bool(requested)
        return {
            "ok": True,
            "核对编号": check_code,
            "差额编号": gap_code,
            "执行编号": execution_code,
            "核对缺失来源范围": scope,
            "实际补采来源": requested,
            "跳过来源": skipped,
            "新建请求": created,
            "重复请求": duplicates,
            "核对": get_schedule_check(check_code),
        }


def get_schedule_check(check_code: str) -> dict[str, Any]:
    row = _check_row(check_code)
    if row is None:
        raise GapError(f"漏采核对记录 {check_code} 不存在")
    return {
        **row,
        "缺失来源范围": _loads(row.get("缺失来源范围"), []),
        "补采请求": _loads(row.get("补采请求"), []),
        "补采结果": _loads(row.get("补采结果"), []),
    }


# ── 演示场景 ──────────────────────────────────────────────────

DEMO_SOURCE_COUNT = 10
DEMO_ISOLATED_ROW = 9
DEMO_LATE_ROW = 8
DEMO_CONFIRMED_ROW = 10


def _demo_code() -> str:
    existing = [
        str(row.get("执行编号", ""))
        for row in store.rows("flow_run")
        if str(row.get("执行编号", "")).startswith("RUN-GAP-DEMO-")
    ]
    return f"RUN-GAP-DEMO-{len(existing) + 1:02d}"


def seed_demo_execution(plan_code: str = "SCHE-DEMO") -> str:
    """生成 10 行演示执行：第 8 行迟到、第 9 行校验隔离、第 10 行确认缺失。

    场景停在“漏采核对已生成、8 和 10 已进入领用窗口且尚未取数”的状态，方便
    随后插入第 8 行的有效迟报，观察旧领用如何收口、排程如何跳过第 8 行继续补
    第 10 行。
    """
    execution_code = _demo_code()
    store.rows("flow_run").append({
        "id": _next_id("flow_run"),
        "status": "已完成",
        "pending": False,
        "abnormal": False,
        "执行编号": execution_code,
        "关联模板": "FLOW-0003",
        "执行节点": "ROBO-0002",
        "触发方式": "排程触发",
        "开始时间": _now_iso(),
        "结束时间": _now_iso(),
        "步骤进度": "10/10",
        "产出记录数": str(DEMO_SOURCE_COUNT),
    })
    records = [
        {
            "来源编号": f"SRC-{index:02d}",
            "行号": index,
            "证据内容": f'{{"url":"https://example{index}.com/data","row":{index}}}',
            "校验状态": STATE_ISOLATED if index == DEMO_ISOLATED_ROW else "校验通过",
        }
        for index in range(1, DEMO_SOURCE_COUNT + 1)
    ]
    record_capture(execution_code, records, batch_code=f"CAPT-{execution_code}")
    record_receipts(
        execution_code,
        [f"SRC-{index:02d}" for index in range(1, 8)],
        handoff_code=f"HAND-{execution_code}",
    )
    snap = create_gap(execution_code, reason="演示执行首次差额")
    confirm_missing(snap["差额编号"], [f"SRC-{DEMO_CONFIRMED_ROW:02d}"], operator="值班员")
    claim_missing(
        snap["差额编号"],
        [f"SRC-{DEMO_LATE_ROW:02d}", f"SRC-{DEMO_CONFIRMED_ROW:02d}"],
        operator="值班员",
    )
    create_schedule_check(snap["差额编号"], plan_code=plan_code, operator="值班员")
    return execution_code
