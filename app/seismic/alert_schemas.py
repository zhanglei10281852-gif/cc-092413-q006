from __future__ import annotations

from pydantic import BaseModel, Field

from app.seismic.alert_state import SOURCE_AUTOMATIC, SOURCE_MANUAL

_ALERT_STATUS_PATTERN = r"^(detected|pending|published|escalated|released|voided)$"
_ACTION_PATTERN = r"^(submit_confirm|publish|escalate|release|void)$"
_SOURCE_PATTERN = rf"^({SOURCE_AUTOMATIC}|{SOURCE_MANUAL})$"


class AlertDetectRequest(BaseModel):
    request_key: str | None = Field(default=None, min_length=1, max_length=120)
    trigger_source: str = Field(default=SOURCE_AUTOMATIC, pattern=_SOURCE_PATTERN)
    actor: str | None = Field(default=None, max_length=80)
    detail: dict = Field(default_factory=dict)


class AlertTransitionRequest(BaseModel):
    action: str = Field(..., pattern=_ACTION_PATTERN)
    expected_version: int | None = Field(default=None, ge=1)
    trigger_source: str = Field(default=SOURCE_MANUAL, pattern=_SOURCE_PATTERN)
    reason: str = Field(default="", max_length=300)
    detail: dict = Field(default_factory=dict)
