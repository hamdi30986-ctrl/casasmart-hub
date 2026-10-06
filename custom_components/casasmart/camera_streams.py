"""Camera stream tickets + HLS path validation (B16 stage 3c-3): pure half.

The camera fallback player loads the HLS playlist inside a WebView — it
cannot attach an ``Authorization`` header to the playlist or segment
fetches. So the authenticated mint endpoint (``camera_api.py``) issues a
short-lived single-entity *ticket* that rides in the URL path instead,
exactly the way HA's own ``/api/hls/<token>/...`` works. This module is
the HA-import-free half — ticket lifecycle and HLS filename validation —
so the unit tests exercise every rejection branch without an HA install,
same split as ``history`` / ``entity_bridge``.

A ticket is NOT a session token: it grants exactly one camera's HLS
files, for minutes, and nothing else. Scope was already enforced at mint
time by the JWT gate; the ticket just carries that one decision to the
header-less fetches that follow.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

# A fullscreen viewing session re-mints on open, so tickets only need to
# outlive one continuous watch. Long enough that a stable stream never
# dies mid-watch, short enough that a leaked playlist URL goes cold fast.
TICKET_TTL = 15 * 60.0

# Outstanding-ticket cap — GLOBAL across all cameras, not per-camera.
# Every fullscreen open is one mint, so anything near this is a
# misbehaving client or probing; soonest-to-expire gets evicted, and a
# watcher whose ticket is evicted just re-mints on its next open.
MAX_TICKETS = 64

# What an HLS filename may look like. HA's hls provider serves exactly:
# master_playlist.m3u8 / playlist.m3u8 / init.mp4 /
# segment/<seq>.m4s / segment/<seq>.<part>.m4s — one optional directory
# level, simple dotted names. No "..", no absolute paths, no schemes.
_HLS_FILENAME = re.compile(r"^[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+(?:\.[0-9]+)?)?\.[A-Za-z0-9]+$")


class TicketError(Exception):
    """A ticket or HLS path was rejected; str(err) says why (log-facing)."""


@dataclass(frozen=True)
class StreamTicket:
    """One minted grant: this ticket id may fetch this entity's HLS files."""

    ticket_id: str
    entity_id: str
    expires_at: float


def is_valid_hls_filename(filename: str) -> bool:
    """True when ``filename`` is a plausible HA HLS artifact path.

    This is the proxy's traversal gate — anything that isn't a plain
    ``name.ext`` or ``dir/name.ext`` (HA's ``segment/`` level) is refused
    before it can reach the loopback URL join.
    """
    return bool(_HLS_FILENAME.match(filename))


class StreamTicketStore:
    """In-memory mint/validate store for camera stream tickets.

    Purely synchronous and clock-injected: callers pass ``now`` (the view
    passes ``time.time()``), so expiry logic is deterministic under test.
    In-memory only — a hub restart drops all tickets, which is correct:
    the player just re-mints on its next open.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, StreamTicket] = {}

    def mint(self, entity_id: str, *, now: float) -> StreamTicket:
        """Issue a fresh ticket for ``entity_id``; evicts expired first."""
        self._purge(now)
        if len(self._tickets) >= MAX_TICKETS:
            # Drop the soonest-to-expire grant — a full table under the cap
            # means a client gone wrong, not a legitimate 65th viewer.
            oldest = min(self._tickets.values(), key=lambda t: t.expires_at)
            del self._tickets[oldest.ticket_id]
        ticket = StreamTicket(
            ticket_id=secrets.token_urlsafe(24),
            entity_id=entity_id,
            expires_at=now + TICKET_TTL,
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def validate(self, ticket_id: str, entity_id: str, *, now: float) -> None:
        """Raise [TicketError] unless this ticket grants this entity, now.

        A ticket for camera A presented on camera B's URL is the same
        rejection as an unknown ticket — nothing to enumerate.
        """
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.entity_id != entity_id:
            raise TicketError("Unknown stream ticket")
        if now >= ticket.expires_at:
            del self._tickets[ticket_id]
            raise TicketError("Stream ticket expired")

    def _purge(self, now: float) -> None:
        expired = [
            ticket_id
            for ticket_id, ticket in self._tickets.items()
            if now >= ticket.expires_at
        ]
        for ticket_id in expired:
            del self._tickets[ticket_id]

    def __len__(self) -> int:
        return len(self._tickets)
