"""采集差额视图路由：采集证据、交接回执、差额快照、领用、补采与排程核对。

固定路径必须注册在通用资源路由 `/api/{module}` 之前，避免被动态模块名抢匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app import gap
from app.schemas import ActionResult

router = APIRouter(prefix="/api", tags=["采集差额"])


def _guard(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except gap.GapError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/gap")
def list_gap_overview() -> dict[str, Any]:
    """差额总览：列出有采集证据的执行及其当前差额。"""
    execution_codes = sorted({
        str(row.get("执行编号", ""))
        for row in gap.store.rows(gap.EVIDENCE_TABLE)
        if row.get("执行编号")
    })
    executions = []
    for code in execution_codes:
        view = gap.current_view(code)
        executions.append({
            "执行编号": code,
            "执行产出记录数": view["执行产出记录数"],
            "摘要": view["摘要"],
            "差额版本": view["差额版本"],
        })
    return {"items": executions, "total": len(executions)}


@router.get("/gap/{execution_code}")
def get_current_gap_view(execution_code: str) -> dict[str, Any]:
    """当前差额视图：按实际采集记录逐行关联各阶段。"""
    return _guard(gap.current_view, execution_code)


@router.post("/gap/{execution_code}/capture")
def register_capture(execution_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """登记执行实际报出的采集记录；`records` 每项含来源编号、行号、校验状态。"""
    return _guard(
        gap.record_capture,
        execution_code,
        payload.get("records", []),
        batch_code=payload.get("batch_code"),
        captured_at=payload.get("captured_at"),
        source_type=payload.get("source_type", "原始采集"),
    )


@router.post("/gap/{execution_code}/receipts")
def register_receipts(execution_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """登记交接回执，支持 `late=true` 的迟到有效回执。"""
    return _guard(
        gap.record_receipts,
        execution_code,
        payload.get("sources", []),
        handoff_code=payload.get("handoff_code", ""),
        late=bool(payload.get("late", False)),
        received_at=payload.get("received_at"),
    )


@router.post("/gap/{execution_code}/snapshots")
def create_snapshot(execution_code: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """按当前证据与回执冻结一版差额。"""
    reason = (payload or {}).get("reason", "手动生成")
    return _guard(gap.create_gap, execution_code, reason=reason)


@router.get("/gap-snapshot/{gap_code}")
def get_snapshot(gap_code: str) -> dict[str, Any]:
    return _guard(gap.get_gap, gap_code)


@router.post("/gap-snapshot/{gap_code}/recalc")
def recalc_snapshot(gap_code: str) -> dict[str, Any]:
    """只用原采集证据复算该版差额，并给出与当前实时状态的差异。"""
    return _guard(gap.recalc_gap, gap_code)


@router.post("/gap-snapshot/{gap_code}/confirm")
def confirm_snapshot_missing(gap_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把待核来源确认为缺失，形成补采范围。"""
    return _guard(
        gap.confirm_missing,
        gap_code,
        payload.get("sources", []),
        payload.get("operator", "值班员"),
    )


@router.post("/gap-snapshot/{gap_code}/claims")
def claim_snapshot(gap_code: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """在领用窗口里认领缺失来源，重复认领不会产生第二笔。"""
    payload = payload or {}
    return _guard(
        gap.claim_missing,
        gap_code,
        payload.get("sources"),
        payload.get("operator", "值班员"),
    )


@router.get("/gap-claims")
def get_claim_window(
    gap_code: str | None = Query(default=None),
    execution_code: str | None = Query(default=None),
    include_closed: bool = Query(default=False),
) -> dict[str, Any]:
    return gap.list_claim_window(
        gap_code=gap_code,
        execution_code=execution_code,
        include_closed=include_closed,
    )


@router.post("/gap-snapshot/{gap_code}/requests")
def create_replenish_requests(gap_code: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """提交补采请求；同一来源重复提交只保留一笔。"""
    payload = payload or {}
    return _guard(
        gap.request_replenish,
        gap_code,
        payload.get("sources"),
        payload.get("operator", "值班员"),
        check_code=payload.get("check_code", ""),
    )


@router.post("/gap-request/{request_code}/complete")
def complete_request(request_code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """兑现补采请求：写入补采证据，并把结果关联回差额与核对记录。"""
    return _guard(
        gap.complete_replenish,
        request_code,
        payload.get("records", []),
        payload.get("operator", "补采执行器"),
    )


@router.post("/gap-snapshot/{gap_code}/checks")
def create_check_record(gap_code: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """把差额核对结果写入排程的漏采核对记录。"""
    payload = payload or {}
    return _guard(
        gap.create_schedule_check,
        gap_code,
        plan_code=payload.get("plan_code", ""),
        operator=payload.get("operator", "值班员"),
    )


@router.get("/gap-check/{check_code}")
def get_check_record(check_code: str) -> dict[str, Any]:
    return _guard(gap.get_schedule_check, check_code)


@router.post("/gap-check/{check_code}/dispatch")
def dispatch_check_record(check_code: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """排程按核对记录发起实际补采，已由迟报补齐的来源不会重新归集。"""
    payload = payload or {}
    return _guard(gap.dispatch_check, check_code, payload.get("operator", "排程补采"))


@router.post("/gap-demo/seed")
def seed_demo_gap() -> ActionResult:
    """生成一份 10 行执行演示：第 8 行迟到、第 9 行隔离、第 10 行确认缺失。"""
    code = gap.seed_demo_execution()
    return ActionResult(ok=True, message=f"演示执行 {code} 已生成", entry={"执行编号": code})
