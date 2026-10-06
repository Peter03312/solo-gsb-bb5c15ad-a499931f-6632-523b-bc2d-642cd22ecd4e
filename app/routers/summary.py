"""合规汇总查询：按分区 + 采样时刻左闭右开区间统计最新已发布报告。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from .. import crud, timeutil
from ..errors import UnprocessableError

router = APIRouter(prefix="/compliance", tags=["compliance"])


def _parse_aware(raw: str, field: str) -> datetime:
    """解析查询参数时刻：要求 ISO8601 且**必须带时区**，失败走统一错误信封。"""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise UnprocessableError(
            f"{field} must be an ISO8601 datetime, got {raw!r}",
            code="invalid_datetime",
            details={"field": field, "value": raw},
        )
    if dt.tzinfo is None:
        raise UnprocessableError(
            f"{field} must be timezone-aware, e.g. 2025-06-01T00:00:00Z",
            code="timezone_required",
            details={"field": field, "value": raw},
        )
    return timeutil.to_utc(dt)


@router.get("/summary")
def compliance_summary(
    partition: str = Query(min_length=1, max_length=64, description="分区编码"),
    start: str = Query(description="采样时刻范围起点（含），ISO8601 且必须带时区"),
    end: str = Query(description="采样时刻范围终点（不含），ISO8601 且必须带时区"),
):
    start_dt = _parse_aware(start, "start")
    end_dt = _parse_aware(end, "end")
    if start_dt >= end_dt:
        raise UnprocessableError(
            "start must be strictly earlier than end (range is half-open [start, end))",
            code="invalid_time_range",
            details={"start": timeutil.iso_z(start_dt), "end": timeutil.iso_z(end_dt)},
        )
    return crud.compliance_summary(
        partition, timeutil.epoch_ms(start_dt), timeutil.epoch_ms(end_dt)
    )
