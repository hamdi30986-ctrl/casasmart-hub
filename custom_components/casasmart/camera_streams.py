"""Camera stream tickets and HLS path validation, free of HA imports.

The app plays the HLS fallback in a WebView, which can't send an
Authorization header. The mint endpoint in camera_api.py checks the token
and room scope, then issues a short-lived ticket that rides in the URL path,
as in HA's own /api/hls/<token>/ URLs. A ticket grants one camera's HLS files
and nothing else, and records the device that minted it so the proxy can
refuse it once that device loses access.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

# The player mints a ticket each time it opens, so one only has to outlast a
# single viewing; a leaked URL still goes cold quickly.
TICKET_TTL = 15 * 60.0

# Cap on live tickets across all cameras. Each open mints one, so reaching it
# means a misbehaving client; the ticket closest to expiry is evicted.
MAX_TICKETS = 64

# HA's HLS provider serves master_playlist.m3u8, playlist.m3u8, init.mp4,
# segment/<seq>.m4s and segment/<seq>.<part>.m4s: at most one directory and
# no "..", absolute paths or schemes.
_HLS_FILENAME = re.compile(
    r"^[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+(?:\.[0-9]+)?)?\.[A-Za-z0-9]+$"
)


class TicketError(Exception):
    """A ticket or HLS path was rejected."""


@dataclass(frozen=True)
class StreamTicket:
    """A grant to fetch one camera's HLS files until expires_at.

    device_id, ver and rooms are the minting device, its auth version and its
    room scope (None for every room) at mint time.
    """

    ticket_id: str
    entity_id: str
    expires_at: float
    device_id: str
    ver: int
    rooms: tuple[str, ...] | None


def is_valid_hls_filename(filename: str) -> bool:
    """True when filename looks like an HA HLS file.

    This is the proxy's path-traversal check before it builds the loopback URL.
    """
    return bool(_HLS_FILENAME.match(filename))


class StreamTicketStore:
    """In-memory store of camera stream tickets.

    Callers pass the current time. A restart drops every ticket; the player
    mints a new one on its next open.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, StreamTicket] = {}

    def mint(
        self,
        entity_id: str,
        *,
        now: float,
        device_id: str,
        ver: int,
        rooms: Iterable[str] | None,
    ) -> StreamTicket:
        """Issue a ticket for entity_id to the device that asked for it."""
        self._purge(now)
        if len(self._tickets) >= MAX_TICKETS:
            oldest = min(self._tickets.values(), key=lambda t: t.expires_at)
            del self._tickets[oldest.ticket_id]
        ticket = StreamTicket(
            ticket_id=secrets.token_urlsafe(24),
            entity_id=entity_id,
            expires_at=now + TICKET_TTL,
            device_id=device_id,
            ver=ver,
            rooms=tuple(rooms) if rooms is not None else None,
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def validate(self, ticket_id: str, entity_id: str, *, now: float) -> StreamTicket:
        """Return the ticket if it is live and grants entity_id, else raise TicketError.

        A ticket for another camera gets the same error as an unknown one.
        """
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.entity_id != entity_id:
            raise TicketError("Unknown stream ticket")
        if now >= ticket.expires_at:
            del self._tickets[ticket_id]
            raise TicketError("Stream ticket expired")
        return ticket

    def discard(self, ticket_id: str) -> None:
        """Forget a ticket whose device has lost access."""
        self._tickets.pop(ticket_id, None)

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
