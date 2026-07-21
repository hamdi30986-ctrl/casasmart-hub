"""``persistent_notification`` stand-in (auth_api's recovery-code surface).

``async_create`` records instead of notifying; ``created`` is introspectable
(and clearable) if a test ever wants to assert on it.
"""

from __future__ import annotations

created: list[dict] = []


def async_create(hass, message, title=None, notification_id=None) -> None:
    created.append(
        {"message": message, "title": title, "notification_id": notification_id}
    )
