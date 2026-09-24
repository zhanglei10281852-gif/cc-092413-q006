from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.seismic.alert_schemas import AlertDetectRequest, AlertTransitionRequest
from app.seismic.alerts import AlertService
from app.seismic.statemachine import AlertAction

router = APIRouter(prefix="/api/seismic/alerts", tags=["地震预警生命周期"])


def service() -> AlertService:
    return AlertService()


@router.post("/detect", status_code=201)
def detect_alert(payload: AlertDetectRequest):
    """自动检测入口：相同事件键的重复检测幂等，不影响既有生命周期。"""
    return service().detect(payload.model_dump())


@router.get("")
def list_alerts(
    state: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
):
    return service().list_alerts(state=state, limit=size, offset=(page - 1) * size)


@router.get("/{alert_id}")
def get_alert(alert_id: int):
    """当前状态、版本、最后一次合法转移、最近一次被拒绝操作的原因。"""
    return service().get_alert(alert_id)


@router.get("/{alert_id}/transitions")
def list_transitions(alert_id: int, limit: int = Query(100, ge=1, le=500)):
    return {"data": service().list_transitions(alert_id, limit=limit)}


@router.post("/{alert_id}/transitions")
def transition_alert(
    alert_id: int,
    payload: AlertTransitionRequest,
    principal: Principal = Depends(current_principal),
):
    return service().transition(
        alert_id,
        AlertAction(payload.action),
        principal=principal,
        expected_version=payload.expected_version,
        level=payload.level,
        reason=payload.reason,
        request_id=payload.request_id,
    )
