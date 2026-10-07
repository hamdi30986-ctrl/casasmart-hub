"""``persistent_notification`` stand-in (auth_api's recovery-code surface).

``async_create`` and ``async_dismiss`` record instead of notifying; ``created``
and ``dismissed`` are introspectable (and clearable) if a test wants them.
"""

from __future__ import annotations

created: list[dict] = []


def async_create(hass, message, title=None, notification_id=None) -> None:
    created.append(
        {"message": message, "title": title, "notification_id": notification_id}
    )


dismissed: list[str] = []


def async_dismiss(hass, notification_id) -> None:
    dismissed.append(notification_id)
