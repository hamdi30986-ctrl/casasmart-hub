"""CasaSmart runtime component."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping





MAX_HISTORY_ENTITIES = 50
MAX_HISTORY_RANGE = timedelta(days=35)


class HistoryQueryError(Exception):
    """CasaSmart runtime component."""


def _parse_timestamp(raw: str, param: str) -> datetime:
    """CasaSmart runtime component."""
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
    return value.astimezone(timezone.utc)


def parse_history_query(
    params: Mapping[str, str], *, now: datetime
) -> tuple[list[str], datetime, datetime, bool]:
    """CasaSmart runtime component."""
    raw_entities = params.get("entities", "")
    entity_ids = [e.strip() for e in raw_entities.split(",") if e.strip()]
    if not entity_ids:
        raise HistoryQueryError("Missing 'entities' (comma-separated entity ids)")
    if len(entity_ids) > MAX_HISTORY_ENTITIES:
        raise HistoryQueryError(
            f"Too many entities: {len(entity_ids)} > {MAX_HISTORY_ENTITIES}"
        )


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
    """CasaSmart runtime component."""
    if isinstance(point, Mapping):
        state = point.get("state")
        changed = point.get("last_changed")
    else:
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
    """CasaSmart runtime component."""
    history: dict[str, list[dict[str, str]]] = {}
    for entity_id in entity_ids:
        points = []
        for point in states.get(entity_id, ()):
            serialized = serialize_history_point(point)
            if serialized is not None:
                points.append(serialized)
        history[entity_id] = points
    return history
