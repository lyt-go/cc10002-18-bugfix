"""发布批次接口：字典版本与元数据属性通过批次号串联，用于两端核对与审计追溯。

读取类接口放在 /api/releases，避免与 /api/dictionary 的 /{entry_id} 路径冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import PageResult
from app.services.release import ReleaseService

router = APIRouter(prefix="/api/releases", tags=["发布批次"])

service = ReleaseService()


@router.get("", response_model=PageResult[dict])
def list_batches(
    keyword: str | None = Query(default=None, description="按批次编号或字典条目编号检索"),
    status: str | None = Query(default=None, description="发布成功、发布失败等"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按批次编号检索发布批次，确认字典与元数据读取的是同一发布版本。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_batches(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{batch_no}", response_model=dict)
def get_batch(batch_no: str) -> dict[str, Any]:
    """按批次编号读取一次发布的确切内容。"""
    batch = service.get_batch_by_no(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"发布批次 {batch_no} 不存在")
    return batch
