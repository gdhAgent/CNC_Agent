"""
app.schemas.stats —— 高频故障 Top-N 看板的响应模型。

按报警码聚合两个数据源：log.query_logs.detected_codes（查询侧）与
ops.maintenance_logs.alarm_code（维修工单侧）；与 kb.alarms 关联取名称 / 严重度。
Source 类型（query / maintenance）供前端切 Tab。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TopFaultItem(BaseModel):
    """单个报警码的频次统计"""
    code_norm: str = Field(..., description="归一化报警码（如 SV0401）")
    count: int = Field(..., description="出现次数")
    name: str | None = Field(None, description="报警名称（来自 kb.alarms）")
    severity: str | None = Field(None, description="严重度（info/warning/fault/fatal/unknown）")
    brand: str | None = Field(None, description="品牌")
    last_seen_at: datetime | None = Field(None, description="最近出现时间")


class TopFaultsWindow(BaseModel):
    """时间窗口（看板顶部展示）"""
    from_time: datetime | None = None
    to_time: datetime = Field(..., description="通常为 NOW()")
    days: int | None = Field(None, description="回溯天数（与 from_time/to_time 二选一）")


class TopFaultsResponse(BaseModel):
    """GET /api/stats/top-faults 响应"""
    window: TopFaultsWindow
    total_query_logs: int = Field(..., description="窗口内总查询日志数")
    total_maintenance_logs: int = Field(..., description="窗口内总工单数")
    by_query: list[TopFaultItem] = Field(
        ..., description="按查询频次聚合（log.query_logs.detected_codes）"
    )
    by_maintenance: list[TopFaultItem] = Field(
        ..., description="按工单频次聚合（ops.maintenance_logs.alarm_code）"
    )


# 来源类型（前端按此切 Tab / 表格列）
Source = Literal["query", "maintenance"]
