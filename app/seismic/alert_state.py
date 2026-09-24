"""预警生命周期状态机。

把自动检测、人工确认、升级发布与解除/作废区分为显式状态，并通过
``(当前状态, 目标状态)`` 白名单约束允许的转移，避免一个迟到的台站包
把已经解除的警报重新推回生效状态。

状态：
    detected    自动检测（系统刚检出，尚未进入人工流程）
    pending     待确认（值班台人工研判中）
    published   已发布（警报生效中）
    escalated   已升级（高级别警报生效中）
    released    已解除（警报解除，终态）
    voided      已作废（误报/取消，终态）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import ConflictError, PermissionDeniedError, ValidationError

# 触发来源
SOURCE_AUTOMATIC = "automatic"  # 自动检测/台站系统
SOURCE_MANUAL = "manual"  # 值班员人工
SOURCES = frozenset({SOURCE_AUTOMATIC, SOURCE_MANUAL})


class AlertStatus(StrEnum):
    DETECTED = "detected"
    PENDING = "pending"
    PUBLISHED = "published"
    ESCALATED = "escalated"
    RELEASED = "released"
    VOIDED = "voided"


TERMINAL_STATUSES = frozenset({AlertStatus.RELEASED, AlertStatus.VOIDED})


@dataclass(frozen=True, slots=True)
class AlertTransition:
    source: AlertStatus
    target: AlertStatus
    action: str
    required_permission: str | None
    allowed_sources: frozenset[str]


# key: (当前状态, 目标状态)
TRANSITIONS: dict[tuple[AlertStatus, AlertStatus], AlertTransition] = {
    # 自动检测 -> 待人工确认（检测算法或人工触发均可）
    (AlertStatus.DETECTED, AlertStatus.PENDING): AlertTransition(
        AlertStatus.DETECTED, AlertStatus.PENDING, "submit_confirm",
        "seismic.alert.confirm", SOURCES,
    ),
    # 值班员确认后首次发布
    (AlertStatus.PENDING, AlertStatus.PUBLISHED): AlertTransition(
        AlertStatus.PENDING, AlertStatus.PUBLISHED, "publish",
        "seismic.alert.publish", SOURCES,
    ),
    # 已发布升级为更高级别警报
    (AlertStatus.PUBLISHED, AlertStatus.ESCALATED): AlertTransition(
        AlertStatus.PUBLISHED, AlertStatus.ESCALATED, "escalate",
        "seismic.alert.publish", SOURCES,
    ),
    # 升级后可继续逐级升级
    (AlertStatus.ESCALATED, AlertStatus.ESCALATED): AlertTransition(
        AlertStatus.ESCALATED, AlertStatus.ESCALATED, "escalate",
        "seismic.alert.publish", SOURCES,
    ),
    # 发布 / 升级后解除警报
    (AlertStatus.PUBLISHED, AlertStatus.RELEASED): AlertTransition(
        AlertStatus.PUBLISHED, AlertStatus.RELEASED, "release",
        "seismic.alert.release", SOURCES,
    ),
    (AlertStatus.ESCALATED, AlertStatus.RELEASED): AlertTransition(
        AlertStatus.ESCALATED, AlertStatus.RELEASED, "release",
        "seismic.alert.release", SOURCES,
    ),
    # 检测 / 待确认阶段发现误报，作废（警报从未生效）
    (AlertStatus.DETECTED, AlertStatus.VOIDED): AlertTransition(
        AlertStatus.DETECTED, AlertStatus.VOIDED, "void",
        "seismic.alert.release", SOURCES,
    ),
    (AlertStatus.PENDING, AlertStatus.VOIDED): AlertTransition(
        AlertStatus.PENDING, AlertStatus.VOIDED, "void",
        "seismic.alert.release", SOURCES,
    ),
}

# 每个状态允许的合法目标，供查询接口透出
ALLOWED_TARGETS: dict[AlertStatus, frozenset[AlertStatus]] = {
    status: frozenset(target for (source, target) in TRANSITIONS if source == status)
    for status in AlertStatus
}

CanFn = Callable[[str], bool]


def resolve_status(value: str) -> AlertStatus:
    try:
        return AlertStatus(value)
    except ValueError as exc:
        raise ValidationError(
            f"未知预警状态：{value}",
            context={"allowed": [status.value for status in AlertStatus]},
        ) from exc


def check_transition(
    source: AlertStatus,
    target: AlertStatus,
    *,
    trigger_source: str,
    can: CanFn,
) -> AlertTransition:
    """校验一次转移是否合法，不合法则抛出带原因的领域异常。

    依次校验：触发来源 -> 状态转移白名单（终态拒收）-> 操作权限。
    """
    if trigger_source not in SOURCES:
        raise ValidationError(
            f"未知触发来源：{trigger_source}",
            context={"reason_code": "unknown_source", "allowed": sorted(SOURCES)},
        )
    transition = TRANSITIONS.get((source, target))
    if transition is None:
        if source in TERMINAL_STATUSES:
            raise ConflictError(
                f"预警已处于终态 {source.value}，不能转移到 {target.value}",
                context={"reason_code": "terminal_state", "current": source.value, "target": target.value},
            )
        raise ConflictError(
            f"状态不允许从 {source.value} 转到 {target.value}",
            context={"reason_code": "illegal_transition", "current": source.value, "target": target.value},
        )
    if trigger_source not in transition.allowed_sources:
        raise PermissionDeniedError(
            f"{transition.action} 不允许由 {trigger_source} 触发",
            context={"reason_code": "source_not_allowed", "allowed": sorted(transition.allowed_sources)},
        )
    if transition.required_permission is not None and not can(transition.required_permission):
        raise PermissionDeniedError(
            f"缺少权限：{transition.required_permission}",
            context={"reason_code": "missing_permission", "required": transition.required_permission},
        )
    return transition
