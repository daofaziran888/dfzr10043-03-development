"""采集差额视图关键链路测试：真实 store、10 行执行、迟到回执与补采幂等。"""
from __future__ import annotations

import pytest

from app import gap
from app.services import service_for


def _rows_of(view: dict) -> dict[str, dict]:
    return {row["来源编号"]: row for row in view["来源明细"]}


def _bare_execution(code: str = "RUN-BARE-01") -> str:
    """10 行采集、7 行回执，但没有确认、领用与核对记录，便于从零验证窗口行为。"""
    gap.store.rows("flow_run").append({
        "id": 900, "status": "已完成", "pending": False, "abnormal": False,
        "执行编号": code, "关联模板": "FLOW-0003", "执行节点": "ROBO-0002",
        "触发方式": "排程触发", "开始时间": "2026-11-24", "结束时间": "2026-11-24",
        "步骤进度": "10/10", "产出记录数": "10",
    })
    gap.record_capture(code, [
        {
            "来源编号": f"SRC-{i:02d}", "行号": i, "证据内容": f"裸执行第{i}行",
            "校验状态": "校验隔离" if i == 9 else "校验通过",
        }
        for i in range(1, 11)
    ])
    gap.record_receipts(code, [f"SRC-{i:02d}" for i in range(1, 8)])
    return code


class TestGapView:
    """差额视图需按实际采集记录关联各阶段，而不是只报产出总数。"""

    def test_ten_row_execution_produces_stage_breakdown(self):
        code = gap.seed_demo_execution()
        view = gap.current_view(code)
        summary = view["摘要"]
        assert view["执行产出记录数"] == "10"
        assert summary["采集总数"] == 10
        assert summary["交接总数"] == 7
        assert summary["差额总数"] == 3
        assert summary["已确认差额"] == 1
        assert summary["待核"] == 1
        assert summary["隔离"] == 1
        assert summary["已确认差额来源"] == ["SRC-10"]
        assert summary["待核来源"] == ["SRC-08"]
        assert summary["隔离来源"] == ["SRC-09"]

    def test_isolated_source_is_not_replenishable(self):
        code = gap.seed_demo_execution()
        rows = _rows_of(gap.current_view(code))
        assert rows["SRC-09"]["阶段"] == "校验隔离"
        assert rows["SRC-09"]["是否可补采"] is False
        assert rows["SRC-09"]["交接阶段"]["接收状态"] == ""

    def test_isolated_source_rejects_receipt(self):
        code = gap.seed_demo_execution()
        with pytest.raises(gap.GapError):
            gap.record_receipts(code, ["SRC-09"])

    def test_other_execution_same_source_does_not_offset_gap(self):
        code = gap.seed_demo_execution()
        store = gap.store
        store.rows("flow_run").append({
            "id": 999, "status": "已完成", "pending": False, "abnormal": False,
            "执行编号": "RUN-OTHER-01", "关联模板": "FLOW-0003",
            "执行节点": "ROBO-0002", "触发方式": "排程触发",
            "开始时间": "2026-11-24", "结束时间": "2026-11-24",
            "步骤进度": "10/10", "产出记录数": "10",
        })
        gap.record_capture("RUN-OTHER-01", [
            {"来源编号": f"SRC-{i:02d}", "行号": i, "证据内容": "other"}
            for i in range(1, 11)
        ])
        gap.record_receipts("RUN-OTHER-01", [f"SRC-{i:02d}" for i in range(1, 11)])
        summary = gap.current_view(code)["摘要"]
        assert summary["交接总数"] == 7
        assert summary["差额总数"] == 3
        other = gap.current_view("RUN-OTHER-01")["摘要"]
        assert other["差额总数"] == 0

    def test_view_is_independent_per_execution_code(self):
        code = gap.seed_demo_execution()
        other = gap.seed_demo_execution()
        assert code != other
        gap.record_receipts(other, ["SRC-08"], late=True)
        assert gap.current_view(other)["摘要"]["待核"] == 0
        assert gap.current_view(code)["摘要"]["待核"] == 1


class TestGapReplay:
    """迟到结果到达后，旧差额仍要用原采集证据复算。"""

    def test_old_gap_replay_keeps_original_numbers(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        gap.record_receipts(code, ["SRC-08"], handoff_code="HAND-LATE", late=True)

        replay = gap.recalc_gap(gap_code)
        assert replay["与原记录一致"] is True
        assert replay["复算"]["交接总数"] == 7
        assert replay["复算"]["差额总数"] == 3
        assert replay["复算"]["待核"] == 1
        assert replay["当前"]["交接总数"] == 8
        assert replay["当前差异"]["交接总数"] == 1
        assert replay["当前差异"]["差额总数"] == -1

    def test_new_gap_version_reflects_late_receipt(self):
        code = gap.seed_demo_execution()
        old = gap.get_gap(gap.current_view(code)["差额版本"][0]["差额编号"])
        gap.record_receipts(code, ["SRC-08"], handoff_code="HAND-LATE", late=True)
        new = gap.regenerate_gap(code)

        assert new["差额编号"] != old["差额编号"]
        assert new["差额版本"] == 2
        assert new["交接总数"] == 8
        assert new["差额总数"] == 2
        assert new["待核来源"] == []
        assert new["已确认差额来源"] == ["SRC-10"]
        assert new["隔离来源"] == ["SRC-09"]
        assert "SRC-08" in new["迟到来源"]
        # 旧差额不被新回执改写
        assert gap.get_gap(old["差额编号"])["交接总数"] == 7
        assert gap.get_gap(old["差额编号"])["待核来源"] == ["SRC-08"]

    def test_late_receipt_keeps_late_marker(self):
        code = gap.seed_demo_execution()
        gap.record_receipts(code, ["SRC-08"], handoff_code="HAND-LATE", late=True)
        rows = _rows_of(gap.current_view(code))
        assert rows["SRC-08"]["阶段"] == "已交接（迟报）"
        assert rows["SRC-08"]["交接阶段"]["迟到"] is True


class TestClaimWindowAndLateReceipt:
    """值班员已认领漏采来源、尚未取数时，迟报到达要收口旧领用。"""

    def test_claim_is_idempotent(self):
        code = _bare_execution()
        gap_code = gap.create_gap(code)["差额编号"]
        first = gap.claim_missing(gap_code, ["SRC-08"], operator="甲")
        second = gap.claim_missing(gap_code, ["SRC-08"], operator="乙")
        assert len(first["新建领用"]) == 1
        assert len(second["新建领用"]) == 0
        assert second["重复领用"] == [first["新建领用"][0]["领用编号"]]
        assert len(gap.list_claim_window(gap_code=gap_code)["领用窗口"]) == 1

    def test_late_receipt_settles_open_claim_and_cancels_request(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        check = gap.create_schedule_check(gap_code)
        dispatched = gap.dispatch_check(check["核对编号"])
        assert set(dispatched["实际补采来源"]) == {"SRC-08", "SRC-10"}

        receipt = gap.record_receipts(code, ["SRC-08"], handoff_code="HAND-LATE", late=True)
        assert receipt["已补齐领用"]
        assert len(receipt["已撤销补采请求"]) == 1

        window = gap.list_claim_window(execution_code=code, include_closed=True)
        status = {row["来源编号"]: row for row in window["领用窗口"]}
        assert status["SRC-08"]["领用状态"] == "已补齐"
        assert status["SRC-08"]["补齐方式"] == "有效迟报"
        assert status["SRC-10"]["领用状态"] == "补采中"

        requests = {row["来源编号"]: row for row in gap._list_requests(execution_code=code)}
        assert requests["SRC-08"]["请求状态"] == "已撤销"
        assert requests["SRC-10"]["请求状态"] == "待执行"

    def test_cancelled_request_cannot_be_completed(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        check_code = gap.create_schedule_check(gap_code)["核对编号"]
        dispatched = gap.dispatch_check(check_code)
        request_code = next(
            row["请求编号"] for row in dispatched["新建请求"] if row["来源编号"] == "SRC-08"
        )
        gap.record_receipts(code, ["SRC-08"], late=True)
        with pytest.raises(gap.GapError):
            gap.complete_replenish(request_code, [
                {"来源编号": "SRC-08", "行号": 8, "证据内容": "late", "校验状态": "校验通过"},
            ])


class TestScheduleCheck:
    """阶段差额核对结果写排程漏采核对，排程按执行编号和来源范围发起补采。"""

    def test_check_records_execution_and_missing_scope(self):
        code = _bare_execution()
        gap.create_gap(code)
        gap.create_gap(code)  # 即便存在多版差额，核对仍按执行编号归拢
        latest_gap = gap.current_view(code)["差额版本"][-1]["差额编号"]
        created = gap.create_schedule_check(latest_gap, plan_code="SCHE-DEMO")
        check = created["核对"]
        assert check["执行编号"] == code
        assert check["差额编号"] == latest_gap
        assert check["缺失来源范围"] == ["SRC-08", "SRC-10"]
        assert "SRC-09" not in check["缺失来源范围"]
        service = service_for("shortfall_check")
        assert service is not None
        matches = service.get_by_execution(code)
        assert created["核对编号"] in [row["核对编号"] for row in matches]

    def test_dispatch_skips_late_source_and_continues_late_claim(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        check_code = gap.create_schedule_check(gap_code)["核对编号"]
        gap.dispatch_check(check_code)

        gap.record_receipts(code, ["SRC-08"], handoff_code="HAND-LATE", late=True)
        result = gap.dispatch_check(check_code)

        assert result["实际补采来源"] == ["SRC-10"]
        assert [row["来源编号"] for row in result["跳过来源"]] == ["SRC-08"]
        assert "迟报" in result["跳过来源"][0]["原因"]
        # 第 8 行不得重新归集：没有为它新建补采请求
        assert all(row["来源编号"] != "SRC-08" for row in result["新建请求"])
        assert all(row["来源编号"] != "SRC-08" for row in result["重复请求"])
        check = gap.get_schedule_check(check_code)
        assert check["核对状态"] == "补采中"
        assert check["status"] == "补采中"

    def test_dispatch_does_not_reclaim_isolated_source(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        # 人工把隔离来源塞进核对范围，模拟上游误写：派发时必须剔除。
        check = gap.create_schedule_check(gap_code)["核对"]
        row = gap._check_row(check["核对编号"])
        row["缺失来源范围"] = gap._dumps(["SRC-09"])
        dispatched = gap.dispatch_check(check["核对编号"])
        assert dispatched["实际补采来源"] == []
        assert dispatched["跳过来源"][0]["来源编号"] == "SRC-09"

    def test_check_links_replenishment_results_back_to_gap(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        check_code = gap.create_schedule_check(gap_code)["核对编号"]
        dispatched = gap.dispatch_check(check_code)
        request_code = next(
            row["请求编号"] for row in dispatched["新建请求"] if row["来源编号"] == "SRC-10"
        )
        result = gap.complete_replenish(request_code, [{
            "来源编号": "SRC-10", "行号": 10, "证据内容": "补采第10行",
            "校验状态": "校验通过", "采集批次": "CAPT-RPL-10",
        }])
        assert result["已经兑现"] is False
        assert result["当前视图"]["交接总数"] == 8

        view = gap.current_view(code)
        assert view["摘要"]["已补采"] == 1
        assert view["摘要"]["已补采来源"] == ["SRC-10"]
        rows = _rows_of(view)
        assert rows["SRC-10"]["差额状态"] == "已补采"

        check = gap.get_schedule_check(check_code)
        assert request_code in check["补采请求"]
        assert any(item["请求编号"] == request_code and item["结果"] == "已兑现"
                   for item in check["补采结果"])
        stored_gap = gap.get_gap(gap_code)
        assert any(item["请求编号"] == request_code for item in stored_gap["补采结果"])


class TestReplenishIdempotency:
    """补采请求同时提交只兑现一次。"""

    def test_duplicate_request_submission_creates_one_request(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        first = gap.request_replenish(gap_code, ["SRC-10"])
        second = gap.request_replenish(gap_code, ["SRC-10"])
        assert len(first["新建请求"]) == 1
        assert len(second["新建请求"]) == 0
        assert second["重复请求"][0]["请求编号"] == first["新建请求"][0]["请求编号"]
        assert len(gap._list_requests(execution_code=code)) == 1

    def test_duplicate_request_across_gap_versions_stays_single(self):
        code = gap.seed_demo_execution()
        old = gap.current_view(code)["差额版本"][0]["差额编号"]
        gap.record_receipts(code, ["SRC-08"], late=True)
        new = gap.regenerate_gap(code)["差额编号"]
        old_result = gap.request_replenish(old, ["SRC-10"])
        new_result = gap.request_replenish(new, ["SRC-10"])
        assert len(old_result["新建请求"]) == 1
        assert new_result["新建请求"] == []
        assert new_result["重复请求"][0]["请求编号"] == old_result["新建请求"][0]["请求编号"]

    def test_request_is_fulfilled_only_once(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        request_code = gap.request_replenish(gap_code, ["SRC-10"])["新建请求"][0]["请求编号"]
        record = {
            "来源编号": "SRC-10", "行号": 10, "证据内容": "补采",
            "校验状态": "校验通过",
        }
        first = gap.complete_replenish(request_code, [record])
        second = gap.complete_replenish(request_code, [record])
        assert first["已经兑现"] is False
        assert second["已经兑现"] is True
        evidence = [
            row for row in gap.store.rows(gap.EVIDENCE_TABLE)
            if row.get("执行编号") == code
            and row.get("来源编号") == "SRC-10"
            and row.get("来源类型") == "补采"
        ]
        assert len(evidence) == 1
        assert second["兑现时间"] == first["兑现时间"]

    def test_request_rejects_already_received_source(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        gap.record_receipts(code, ["SRC-08"], late=True)
        with pytest.raises(gap.GapError):
            gap.request_replenish(gap_code, ["SRC-08"])

    def test_complete_requires_matching_source(self):
        code = gap.seed_demo_execution()
        gap_code = gap.current_view(code)["差额版本"][0]["差额编号"]
        request_code = gap.request_replenish(gap_code, ["SRC-10"])["新建请求"][0]["请求编号"]
        with pytest.raises(gap.GapError):
            gap.complete_replenish(request_code, [
                {"来源编号": "SRC-08", "行号": 8, "证据内容": "x", "校验状态": "校验通过"},
            ])


class TestModuleIsolation:
    """差额内部表不能改变业务模块列表与运营概览。"""

    def test_internal_tables_are_hidden(self):
        store = gap.store
        names = store.module_names()
        assert not any(name.startswith("_") for name in names)
        assert len(names) == 22
        assert "shortfall_check" not in names

    def test_shortfall_check_not_in_overview_modules(self):
        gap.seed_demo_execution()
        modules = [item["name"] for item in gap.store.overview()["modules"]]
        assert len(modules) == 22
        assert "_shortfall_check" not in modules


class TestPipelineLink:
    """链路11：执行产出直接接进差额视图，并自动写排程核对。"""

    def test_capture_shortfall_creates_gap_and_check(self):
        from app.pipeline import capture_shortfall_to_replenish

        run = gap.store.rows("flow_run")[0]
        run["执行编号"] = "RUN-PIPE-GAP"
        run["产出记录数"] = "10"
        records = [
            {
                "来源编号": f"SRC-{i:02d}", "行号": i,
                "证据内容": f"行{i}",
                "校验状态": "校验隔离" if i == 9 else "校验通过",
            }
            for i in range(1, 11)
        ]
        result = capture_shortfall_to_replenish(
            run["id"], records,
            handoff_code="HAND-PIPE",
            receipt_sources=[f"SRC-{i:02d}" for i in range(1, 8)],
            plan_code="SCHE-0003",
            batch_code="CAPT-PIPE",
        )
        assert result["status"] == "gap_created"
        assert result["执行编号"] == "RUN-PIPE-GAP"
        assert result["摘要"]["采集总数"] == 10
        assert result["摘要"]["交接总数"] == 7
        assert result["摘要"]["差额总数"] == 3

        check = gap.get_schedule_check(result["核对编号"])
        assert check["执行编号"] == "RUN-PIPE-GAP"
        assert check["差额编号"] == result["差额编号"]
        assert check["缺失来源范围"] == ["SRC-08", "SRC-10"]
        assert check["关联计划"] == "SCHE-0003"


class TestGapRoutes:
    """路由层动作直接调用也要能跑通（TestClient 依赖未安装，因此直接调函数）。"""

    def test_route_flow_claim_receipt_dispatch(self):
        from app.routers import gap as routes

        seeded = routes.seed_demo_gap()
        code = seeded.entry["执行编号"]
        view = routes.get_current_gap_view(code)
        assert view["摘要"]["差额总数"] == 3

        gap_code = view["差额版本"][0]["差额编号"]
        # 演示场景已认领 8 和 10；重复认领应返回原领用而不是第二笔。
        claimed = routes.claim_snapshot(gap_code, {"sources": ["SRC-10"]})
        assert claimed["新建领用"] == []
        assert claimed["重复领用"]
        assert [c["来源编号"] for c in claimed["领用窗口"]] == ["SRC-08", "SRC-10"]

        receipt = routes.register_receipts(
            code, {"sources": ["SRC-08"], "late": True})
        assert receipt["已补齐领用"]

        check = routes.create_check_record(gap_code, {"plan_code": "SCHE-0003"})
        dispatched = routes.dispatch_check_record(check["核对编号"])
        # 第 8 行已由迟报补齐，排程不得重新归集；第 10 行继续补。
        assert dispatched["实际补采来源"] == ["SRC-10"]
        assert [row["来源编号"] for row in dispatched["跳过来源"]] == ["SRC-08"]

    def test_route_errors_become_http_400(self):
        from fastapi import HTTPException

        from app.routers import gap as routes

        seeded = routes.seed_demo_gap()
        code = seeded.entry["执行编号"]
        gap_code = routes.get_current_gap_view(code)["差额版本"][0]["差额编号"]
        routes.register_receipts(code, {"sources": ["SRC-08"], "late": True})
        with pytest.raises(HTTPException) as excinfo:
            routes.create_replenish_requests(gap_code, {"sources": ["SRC-08"]})
        assert excinfo.value.status_code == 400

    def test_shortfall_check_route_is_available(self):
        from app.routers import gap as routes

        seeded = routes.seed_demo_gap()
        check_code = routes.get_current_gap_view(
            seeded.entry["执行编号"])["差额版本"][0]["关联核对"][0]
        detail = routes.get_check_record(check_code)
        assert detail["核对编号"] == check_code


class TestConcurrentSubmission:
    """同一来源的补采请求并发提交、并发兑现，也只能落一笔、兑现一次。"""

    def test_concurrent_request_creates_single_request(self):
        import threading

        code = _bare_execution("RUN-CONC-01")
        gap.create_gap(code)
        gap_code = gap.current_view(code)["差额版本"][-1]["差额编号"]
        barrier = threading.Barrier(4)
        results: list[dict] = []

        def worker() -> None:
            barrier.wait()
            results.append(gap.request_replenish(gap_code, ["SRC-10"]))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        created = [item for result in results for item in result["新建请求"]]
        assert len(created) == 1
        requests = gap._list_requests(execution_code="RUN-CONC-01")
        assert len(requests) == 1

    def test_concurrent_complete_writes_evidence_once(self):
        import threading

        code = _bare_execution("RUN-CONC-02")
        gap.create_gap(code)
        gap_code = gap.current_view(code)["差额版本"][-1]["差额编号"]
        request_code = gap.request_replenish(gap_code, ["SRC-10"])["新建请求"][0]["请求编号"]
        barrier = threading.Barrier(4)
        outcomes: list[dict] = []

        def worker() -> None:
            barrier.wait()
            outcomes.append(gap.complete_replenish(request_code, [{
                "来源编号": "SRC-10", "行号": 10, "证据内容": "并发补采",
                "校验状态": "校验通过",
            }]))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for item in outcomes if item["已经兑现"] is False) == 1
        evidence = [
            row for row in gap.store.rows(gap.EVIDENCE_TABLE)
            if row.get("执行编号") == "RUN-CONC-02"
            and row.get("来源编号") == "SRC-10"
            and row.get("来源类型") == "补采"
        ]
        assert len(evidence) == 1


class TestReplenishRecordValidation:
    """补采兑现只接受请求对应来源，并沿用原行号，防止脏行混入。"""

    def test_missing_row_number_is_inherited_from_evidence(self):
        code = _bare_execution("RUN-ROW-01")
        gap.create_gap(code)
        gap_code = gap.current_view(code)["差额版本"][-1]["差额编号"]
        request_code = gap.request_replenish(gap_code, ["SRC-10"])["新建请求"][0]["请求编号"]
        gap.complete_replenish(request_code, [{
            "来源编号": "SRC-10", "证据内容": "无行号补采", "校验状态": "校验通过",
        }])
        row = _rows_of(gap.current_view(code))["SRC-10"]
        assert row["行号"] == 10
        assert row["差额状态"] == "已补采"

    def test_replenish_cannot_override_isolated_source(self):
        code = _bare_execution("RUN-ROW-02")
        gap.create_gap(code)
        gap_code = gap.current_view(code)["差额版本"][-1]["差额编号"]
        with pytest.raises(gap.GapError):
            gap.request_replenish(gap_code, ["SRC-09"])
