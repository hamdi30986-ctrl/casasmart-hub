"""History query parsing and serialization, free of HA imports.

The history endpoint serves recorder history to the app's energy screens.
The recorder call itself is in the view in api.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

# The query runs on HA's own database, so it is bounded. The app asks for a
# handful of sensors at a time.
MAX_HISTORY_ENTITIES = 50
MAX_HISTORY_RANGE = timedelta(days=35)


class HistoryQueryError(Exception):
    """A history query parameter was rejected; str(err) is the 400 body."""


def _parse_timestamp(raw: str, param: str) -> datetime:
    """Parse an ISO-8601 timestamp, which must carry a UTC offset."""
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as err:
        raise HistoryQueryError(
            f"Invalid {param!r}: not an ISO-8601 timestamp"
        ) from err
    if value.tzinfo is None:
        raise HistoryQueryError(
            f"Invalid {param!r}: timestamp must include a UTC offset"
        )
    return value.astimezone(UTC)


def parse_history_query(
    params: Mapping[str, str], *, now: datetime
) -> tuple[list[str], datetime, datetime, bool]:
    """Validate the query string into (entity_ids, start, end, significant).

    Raises HistoryQueryError with a message for the caller.
    """
    raw_entities = params.get("entities", "")
    entity_ids = [e.strip() for e in raw_entities.split(",") if e.strip()]
    if not entity_ids:
        raise HistoryQueryError("Missing 'entities' (comma-separated entity ids)")
    if len(entity_ids) > MAX_HISTORY_ENTITIES:
        raise HistoryQueryError(
            f"Too many entities: {len(entity_ids)} > {MAX_HISTORY_ENTITIES}"
        )
    # Every served id is domain.object; anything else never reaches the query.
    for entity_id in entity_ids:
        domain, sep, obj = entity_id.partition(".")
        if not sep or not domain or not obj:
            raise HistoryQueryError(f"Invalid entity id: {entity_id!r}")

    raw_start = params.get("start")
    if raw_start is None:
        raise HistoryQueryError("Missing 'start' (ISO-8601 timestamp)")
    start = _parse_timestamp(raw_start, "start")

    raw_end = params.get("end")
    end = _parse_timestamp(raw_end, "end") if raw_end is not None else now
    if end > now:
        # Clamp so a phone with a skewed clock gets the same answer.
        end = now
    if start >= end:
        raise HistoryQueryError("'start' must be before 'end'")
    if end - start > MAX_HISTORY_RANGE:
        raise HistoryQueryError(
            f"Range too large: maximum is {MAX_HISTORY_RANGE.days} days"
        )

    significant = params.get("significant", "1") not in ("0", "false")
    return entity_ids, start, end, significant


def serialize_history_point(point: Any) -> dict[str, str] | None:
    """One recorder row as {"state", "last_changed"}, or None if it lacks either.

    Accepts a State-like object or a mapping (HA's minimal row shape).
    """
    if isinstance(point, Mapping):
        state = point.get("state")
        changed = point.get("last_changed")
    else:  # duck-typed homeassistant.core.State
        state = getattr(point, "state", None)
        changed = getattr(point, "last_changed", None)
    if not isinstance(state, str):
        return None
    if isinstance(changed, datetime):
        changed = changed.isoformat()
    if not isinstance(changed, str):
        return None
    return {"state": state, "last_changed": changed}


def serialize_history(
    entity_ids: list[str], states: Mapping[str, list[Any]]
) -> dict[str, list[dict[str, str]]]:
    """Map each allowed entity to its serialized points.

    An entity with no rows gets [], so the app can tell "no data" from an id
    the view dropped as unknown or out of scope (which gets no key).
    """
    history: dict[str, list[dict[str, str]]] = {}
    for entity_id in entity_ids:
        points = []
        for point in states.get(entity_id, ()):
            serialized = serialize_history_point(point)
            if serialized is not None:
                points.append(serialized)
        history[entity_id] = points
    return history
