"""地震预警生命周期状态机。

只包含与存储无关的纯转移逻辑，便于单独测试：

    detected（检测）
        ├─ request_confirmation → pending_confirmation（待确认）
        └─ void                 → voided（作废）
    pending_confirmation（待确认）
        ├─ confirm_publish → published（已发布）
        └─ void            → voided（作废）
    published（已发布）
        ├─ escalate → escalated（升级）
        └─ release  → released（解除）
    escalated（升级）
        ├─ escalate → escalated（再次升级，级别必须提高）
        └─ release  → released（解除）
    released（解除） / voided（作废）：终态，不存在任何合法后继转移

终态不允许任何转出，因此迟到的台站检测包不可能把已解除/已作废的
警报重新推回生效状态。
"""

from __future__ import annotations

from enum import Enum

from app.core.security import Principal


class AlertState(str, Enum):
    DETECTED = "detected"
    PENDING_CONFIRMATION = "pending_confirmation"
    PUBLISHED = "published"
    ESCALATED = "escalated"
    RELEASED = "released"
    VOIDED = "voided"


class AlertAction(str, Enum):
    DETECT = "detect"
    REQUEST_CONFIRMATION = "request_confirmation"
    CONFIRM_PUBLISH = "confirm_publish"
    ESCALATE = "escalate"
    RELEASE = "release"
    VOID = "void"


STATE_LABELS: dict[AlertState, str] = {
    AlertState.DETECTED: "检测",
    AlertState.PENDING_CONFIRMATION: "待确认",
    AlertState.PUBLISHED: "已发布",
    AlertState.ESCALATED: "升级",
    AlertState.RELEASED: "解除",
    AlertState.VOIDED: "作废",
}

ACTION_LABELS: dict[AlertAction, str] = {
    AlertAction.DETECT: "自动检测",
    AlertAction.REQUEST_CONFIRMATION: "提交确认",
    AlertAction.CONFIRM_PUBLISH: "确认并发布",
    AlertAction.ESCALATE: "升级",
    AlertAction.RELEASE: "解除",
    AlertAction.VOID: "作废",
}

# 每个动作需要的权限码；DETECT 来自自动检测系统，不需要登录用户。
PERMISSION_REQUIRED: dict[AlertAction, str] = {
    AlertAction.REQUEST_CONFIRMATION: "seismic.alerts.confirm",
    AlertAction.CONFIRM_PUBLISH: "seismic.alerts.publish",
    AlertAction.ESCALATE: "seismic.alerts.publish",
    AlertAction.RELEASE: "seismic.alerts.release",
    AlertAction.VOID: "seismic.alerts.void",
}

TRANSITIONS: dict[AlertState, dict[AlertAction, AlertState]] = {
    AlertState.DETECTED: {
        AlertAction.REQUEST_CONFIRMATION: AlertState.PENDING_CONFIRMATION,
        AlertAction.VOID: AlertState.VOIDED,
    },
    AlertState.PENDING_CONFIRMATION: {
        AlertAction.CONFIRM_PUBLISH: AlertState.PUBLISHED,
        AlertAction.VOID: AlertState.VOIDED,
    },
    AlertState.PUBLISHED: {
        AlertAction.ESCALATE: AlertState.ESCALATED,
        AlertAction.RELEASE: AlertState.RELEASED,
    },
    AlertState.ESCALATED: {
        AlertAction.ESCALATE: AlertState.ESCALATED,
        AlertAction.RELEASE: AlertState.RELEASED,
    },
    AlertState.RELEASED: {},
    AlertState.VOIDED: {},
}

TERMINAL_STATES = frozenset({AlertState.RELEASED, AlertState.VOIDED})


def next_state(state: AlertState, action: AlertAction) -> AlertState | None:
    """返回合法目标状态；不允许的转移返回 None。"""
    return TRANSITIONS[state].get(action)


def allowed_actions(state: AlertState) -> list[AlertAction]:
    return list(TRANSITIONS[state].keys())


def is_terminal(state: AlertState) -> bool:
    return state in TERMINAL_STATES


def rejection_message(state: AlertState, action: AlertAction) -> str:
    """构造非法转移的明确中文原因。"""
    action_label = ACTION_LABELS[action]
    state_label = STATE_LABELS[state]
    if state in TERMINAL_STATES:
        return (
            f"预警已{state_label}且为终态，{action_label}会改变终态，"
            "迟到数据不得使已结束的预警重新生效"
        )
    legal = "、".join(ACTION_LABELS[item] for item in allowed_actions(state)) or "无"
    return f"当前状态为“{state_label}”，不允许执行“{action_label}”，允许的动作为：{legal}"


def require_permission(action: AlertAction, principal: Principal) -> str | None:
    """权限不足时返回原因；具备权限时返回 None。"""
    permission = PERMISSION_REQUIRED[action]
    if principal.can(permission):
        return None
    return f"缺少权限：{permission}（动作“{ACTION_LABELS[action]}”需要授权）"
