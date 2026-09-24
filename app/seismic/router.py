from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.seismic.alert_schemas import AlertDetectRequest, AlertTransitionRequest
from app.seismic.alert_service import AlertService
from app.seismic.schemas import ComputeRequest, EventCreate, EventPatch, ObservationCreate, TaskComplete
from app.seismic.service import SeismicService
from app.services.auth import AuthService
from app.database import get_connection

router = APIRouter(prefix="/api/seismic", tags=["地震科学计算"])


def service() -> SeismicService:
    return SeismicService()


def alert_service() -> AlertService:
    return AlertService()


def optional_principal(authorization: str | None = Header(default=None)) -> Principal | None:
    """台站自动检测可能没有人工会话；有 Bearer 令牌时解析为操作人。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    return AuthService(get_connection()).principal(token)


@router.post("/events", status_code=201)
def create_event(payload: EventCreate):
    try:
        return service().create_event(payload.model_dump(), actor=payload.source)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="external_id 已存在") from exc
        raise


@router.get("/events/{event_id}")
def get_event(event_id: int, include_observations: bool = Query(True)):
    value = service().get_event(event_id, include_observations)
    if value is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return value


@router.patch("/events/{event_id}")
def patch_event(event_id: int, payload: EventPatch):
    try:
        return service().patch_event(event_id, payload.model_dump(exclude_unset=True), actor="operator")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/observations", status_code=201)
def add_observation(event_id: int, payload: ObservationCreate):
    try:
        return service().add_observation(event_id, payload.model_dump(), actor="station")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/computations", status_code=202)
def enqueue(event_id: int, payload: ComputeRequest):
    try:
        return service().enqueue_computation(event_id, payload.model_version, payload.grid_step_km, payload.radius_km, payload.requested_by)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/computations/claim")
def claim(worker_id: str = Query(..., min_length=1)):
    task = service().claim_task(worker_id)
    return {"task": task}


@router.post("/computations/{task_id}/calculate")
def calculate(task_id: int, worker_id: str = Query(..., min_length=1)):
    try:
        return service().calculate_task(task_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="任务不属于该工作者或不存在") from exc


@router.get("/computations/{task_id}")
def get_computation(task_id: int):
    row = service().connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return dict(row)


# ---------------------------------------------------------------------- #
# 预警生命周期
# ---------------------------------------------------------------------- #
@router.post("/events/{event_id}/alerts/detect", status_code=201)
def detect_alert(event_id: int, payload: AlertDetectRequest, principal: Principal | None = Depends(optional_principal)):
    return alert_service().detect(
        event_id,
        request_key=payload.request_key,
        trigger_source=payload.trigger_source,
        actor=principal.display_name if principal else payload.actor,
        detail=payload.detail,
    )


@router.post("/alerts/{alert_id}/transition")
def transition_alert(alert_id: int, payload: AlertTransitionRequest, principal: Principal = Depends(current_principal)):
    # 状态机内部依据动作校验 seismic.alert.confirm/publish/release 权限
    return alert_service().transition(
        alert_id,
        action=payload.action,
        expected_version=payload.expected_version,
        trigger_source=payload.trigger_source,
        principal=principal,
        reason=payload.reason,
        detail=payload.detail,
    )


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: int, principal: Principal = Depends(current_principal)):
    del principal
    return alert_service().get_alert(alert_id)


@router.get("/events/{event_id}/alerts")
def list_event_alerts(event_id: int, principal: Principal = Depends(current_principal)):
    del principal
    return {"data": alert_service().list_for_event(event_id)}
