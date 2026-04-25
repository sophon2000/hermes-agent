"""Plugin-extensible approval card registry.

Lets plugins ship their own confirmation/approval cards (rendered + routed)
without having to fork the platform adapters. Each registered ``ApprovalKind``
carries a ``render_card`` (payload -> card JSON) and an ``on_action``
(button click -> resolved card + side effects).

The existing shell-command ``send_exec_approval`` flow stays untouched —
this is a parallel, additive surface keyed by ``hermes_kind`` in card button
``value`` payloads.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalContext:
    """Passed to ``on_action`` when a user clicks a button on the card."""

    kind: str
    action_id: str
    action_value: Dict[str, Any]
    payload: Dict[str, Any]
    operator_open_id: str
    operator_name: str
    chat_id: str


@dataclass
class ApprovalResult:
    """What the adapter should do after ``on_action`` returns."""

    resolved_card: Optional[Dict[str, Any]] = None
    log_message: Optional[str] = None


RenderCard = Callable[[Dict[str, Any]], Dict[str, Any]]
OnAction = Callable[[ApprovalContext], Awaitable[ApprovalResult]]


@dataclass(frozen=True)
class ApprovalKind:
    kind: str
    render_card: RenderCard
    on_action: OnAction
    description: str = ""


_registry: Dict[str, ApprovalKind] = {}
_lock = threading.RLock()


def register_approval_kind(
    kind: str,
    render_card: RenderCard,
    on_action: OnAction,
    description: str = "",
) -> None:
    """Register a plugin-defined approval kind."""
    if not kind or not isinstance(kind, str):
        raise ValueError("approval kind must be a non-empty string")
    with _lock:
        existing = _registry.get(kind)
        if existing and (
            existing.render_card is not render_card or existing.on_action is not on_action
        ):
            logger.warning("Approval kind %r already registered; overwriting", kind)
        _registry[kind] = ApprovalKind(
            kind=kind,
            render_card=render_card,
            on_action=on_action,
            description=description,
        )
        logger.debug("Registered approval kind: %s", kind)


def get_approval_kind(kind: str) -> Optional[ApprovalKind]:
    with _lock:
        return _registry.get(kind)


def list_approval_kinds() -> Dict[str, ApprovalKind]:
    with _lock:
        return dict(_registry)


def clear_approval_kinds() -> None:
    """Test-only: drop all registrations."""
    with _lock:
        _registry.clear()
