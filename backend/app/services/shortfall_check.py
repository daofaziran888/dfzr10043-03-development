"""排程漏采核对服务：承接采集差额的核对结果，并驱动实际补采。

差额视图把「执行编号 + 缺失来源范围」写进本模块的核对记录；排程按核对
记录里的执行编号和来源范围发起补采，补采结果再关联回这一笔差额。核对
记录不进入业务模块列表（表名以 `_` 前缀的内部表承载），避免改变原有模块
数量和运营概览，但保留完整的查询、登记和动作接口。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class ShortfallCheckService(BaseModuleService):
    MODULE = "_shortfall_check"
    ENTITY = "漏采核对记录"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "核对编号", "关联计划", "执行编号", "差额编号", "缺失来源范围",
        "覆盖来源数", "核对状态", "核对人", "核对时间", "结果摘要",
    ]
    REQUIRED_FIELDS = ["核对编号", "执行编号", "差额编号", "缺失来源范围"]
    STATUS_ORDER = ["待补采", "补采中", "已完成", "已关闭"]
    ACTION_RULES = {"发起补采": "补采中", "完成核对": "已完成", "关闭核对": "已关闭"}
    NEGATIVE_ACTIONS = []

    def get_by_execution(self, execution_code: str) -> list[dict[str, Any]]:
        """排程按执行编号读取漏采核对记录。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("执行编号") == execution_code]

    def get_by_gap(self, gap_code: str) -> list[dict[str, Any]]:
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("差额编号") == gap_code]

    def close_check(self, entry_id: int) -> dict[str, Any]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "reason": "漏采核对记录不存在"}
        self.run_action(entry_id, "关闭核对")
        return {"ok": True, "entry_id": entry_id, "reason": "核对记录已关闭"}
