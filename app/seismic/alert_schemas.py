from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AlertDetectRequest(BaseModel):
    external_event_key: str = Field(..., min_length=1, max_length=120, description="同一检测事件的幂等键，通常为事件唯一编号")
    event_id: int | None = Field(default=None, description="可选，关联已登记的地震事件")
    title: str = Field(default="", max_length=200)
    region: str = Field(default="", max_length=200)
    initial_level: int = Field(default=1, ge=1, le=5)
    source: str = Field(default="auto-detector", max_length=80, description="触发来源，如台站/检测系统标识")
    request_id: str = Field(default="", max_length=120)


class AlertTransitionRequest(BaseModel):
    action: Literal[
        "request_confirmation",
        "confirm_publish",
        "escalate",
        "release",
        "void",
    ]
    expected_version: int | None = Field(default=None, ge=1, description="客户端所依据的预警版本号，过期则拒绝")
    level: int | None = Field(default=None, ge=1, le=5, description="仅升级动作使用，必须高于当前级别")
    reason: str = Field(default="", max_length=500)
    request_id: str = Field(default="", max_length=120, description="转移请求的幂等键")
