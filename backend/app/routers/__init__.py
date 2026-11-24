"""业务路由汇总：差额视图固定路径在前，通用资源路由在后。"""
from __future__ import annotations

from app.routers import gap, resource

# 顺序有意义：/api/{module} 会吞掉所有两段路径，差额固定路径必须排在它前面。
ROUTERS = [gap, resource]
