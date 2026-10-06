"""History query contract (B16 stage 3c-3): parse + serialize, no HA imports.

The history endpoint replaces the app's raw-token ``GET
/api/history/period/<ts>`` call (energy screen's weekly sampling). This
module is the pure half — query-string validation and point
serialization — kept HA-import-free so the unit tests exercise every
rejection branch without an HA install, same split as ``entity_bridge``.

The recorder call itself lives in the view (``api.py``): it needs the
running recorder instance and the executor, neither of which belongs in
a translation layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

# Hard caps — the recorder query runs on the hub's SQLite; an unbounded
# range or entity list is a self-inflicted DoS, not a feature. The app's
# real consumer (weekly energy sampling) asks for ~1h windows over a
# handful of sensors, so these are generous.
MAX_HISTORY_ENTITIES = 50
MAX_HISTORY_RANGE = timedelta(days=35)


class HistoryQueryError(Exception):
    """A history query parameter was rejected; str(err) is the 400 body."""


def _parse_timestamp(raw: str, param: str) -> datetime:
    """Parse one ISO-8601 timestamp; must be timezone-aware.

    A naive timestamp is ambiguous (phone-local? hub-local? UTC?) and the
    recorder stores UTC — reject instead of guessing.
    """
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
    """Validate the query string into ``(entity_ids, start, end, significant)``.

    Raises [HistoryQueryError] with a caller-facing message on any
    rejection. ``now`` is injected (not read from the clock) so the
    bounds logic is deterministic under test.
    """
    raw_entities = params.get("entities", "")
    entity_ids = [e.strip() for e in raw_entities.split(",") if e.strip()]
    if not entity_ids:
        raise HistoryQueryError("Missing 'entities' (comma-separated entity ids)")
    if len(entity_ids) > MAX_HISTORY_ENTITIES:
        raise HistoryQueryError(
            f"Too many entities: {len(entity_ids)} > {MAX_HISTORY_ENTITIES}"
        )
    # Malformed ids never reach the recorder query — every served id is
    # "domain.object", anything else is noise or probing.
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
        # The future holds no history; clamp so a phone with clock skew
        # gets the same answer as an honest one.
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
    """One recorder row → ``{"state", "last_changed"}`` wire dict.

    The recorder's ``minimal_response`` mode returns a full ``State``
    object for each entity's first row and bare dicts after it (its
    documented shape) — normalize both. Rows that can't yield a state
    string and a timestamp are dropped, never half-serialized.
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
    """Recorder result → wire map, one key per ALLOWED requested entity.

    Entities with no recorded rows get an explicit ``[]`` — the app must
    be able to tell "no data in this window" from "the hub dropped my
    entity" (the latter never has a key: unknown/out-of-scope ids are
    omitted by the view before the query, indistinguishable on purpose).
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
