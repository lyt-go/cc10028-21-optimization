"""版本差异与增量交付接口：记录级差异、选交清单、增量包、回执与断点恢复。

挂在通用资源路由之前，避免 /api/doc_version/compare 之类固定路径
被 /api/{module}/{entry_id} 的动态整数路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.doc_version import DocVersionService
from app.services.result_deliver import ResultDeliverService

router = APIRouter(prefix="/api", tags=["版本差异与增量交付"])


class IncrementalRequest(BaseModel):
    doc_id: str
    base_version: str
    target_version: str
    receiver_id: str = "default"
    receiver_has_baseline: bool = True
    baseline_verified: bool = True
    request_id: str | None = None
    fail_after_shards: int | None = Field(default=None, ge=0)


class ResumeRequest(BaseModel):
    request_key: str
    fail_after_shards: int | None = Field(default=None, ge=0)


@router.get("/doc_version/compare")
def compare_versions(
    doc_id: str = Query(...),
    version_a: str = Query(...),
    version_b: str = Query(...),
    receiver_id: str = Query(default="default"),
    receiver_has_baseline: bool = Query(default=True),
    baseline_verified: bool = Query(default=True),
) -> dict[str, Any]:
    """记录级版本差异 + 版本差异选交清单。"""
    return DocVersionService().compare_versions(
        doc_id,
        version_a,
        version_b,
        receiver_id=receiver_id,
        receiver_has_baseline=receiver_has_baseline,
        baseline_verified=baseline_verified,
    )


@router.get("/doc_version/selection")
def selection_manifest(
    doc_id: str = Query(...),
    version_a: str = Query(...),
    version_b: str = Query(...),
    receiver_id: str = Query(default="default"),
    receiver_has_baseline: bool = Query(default=True),
    baseline_verified: bool = Query(default=True),
) -> dict[str, Any]:
    """只返回版本差异选交清单。"""
    return DocVersionService().build_selection_manifest(
        doc_id,
        version_a,
        version_b,
        receiver_id=receiver_id,
        receiver_has_baseline=receiver_has_baseline,
        baseline_verified=baseline_verified,
    )


@router.get("/doc_version/versions")
def list_versions(doc_id: str = Query(...)) -> dict[str, Any]:
    """列出文档的版本快照与记录级附件可用性。"""
    return DocVersionService().list_versions(doc_id)


@router.post("/result_deliver/incremental")
def deliver_incremental(payload: IncrementalRequest) -> dict[str, Any]:
    """按选交清单生成增量数据包，返回回执。"""
    return ResultDeliverService().deliver_incremental(
        payload.doc_id,
        payload.base_version,
        payload.target_version,
        receiver_id=payload.receiver_id,
        receiver_has_baseline=payload.receiver_has_baseline,
        baseline_verified=payload.baseline_verified,
        request_id=payload.request_id,
        fail_after_shards=payload.fail_after_shards,
    )


@router.post("/result_deliver/resume")
def resume_delivery(payload: ResumeRequest) -> dict[str, Any]:
    """识别同一次交付请求并从断点恢复打包。"""
    return ResultDeliverService().resume_delivery(
        payload.request_key, fail_after_shards=payload.fail_after_shards
    )


@router.get("/result_deliver/verify")
def verify_package(
    request_key: str = Query(...),
    baseline_entity_key: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """导入前端点依赖校验。"""
    return ResultDeliverService().verify_package(
        request_key, baseline_entity_keys=baseline_entity_key
    )


@router.get("/result_deliver/receipt")
def get_receipt(request_key: str = Query(...)) -> dict[str, Any]:
    """读取某次交付请求的回执。"""
    return ResultDeliverService().get_receipt(request_key)


@router.get("/result_deliver/receipts")
def list_receipts() -> dict[str, Any]:
    """列出全部交付回执。"""
    return {"items": ResultDeliverService().list_receipts()}
