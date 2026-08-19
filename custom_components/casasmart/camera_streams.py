"""CasaSmart runtime component."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass




TICKET_TTL = 15 * 60.0





MAX_TICKETS = 64





_HLS_FILENAME = re.compile(r"^[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+(?:\.[0-9]+)?)?\.[A-Za-z0-9]+$")


class TicketError(Exception):
    """CasaSmart runtime component."""


@dataclass(frozen=True)
class StreamTicket:
    """CasaSmart runtime component."""

    ticket_id: str
    entity_id: str
    expires_at: float


def is_valid_hls_filename(filename: str) -> bool:
    """CasaSmart runtime component."""
    return bool(_HLS_FILENAME.match(filename))


class StreamTicketStore:
    """CasaSmart runtime component."""

    def __init__(self) -> None:
        self._tickets: dict[str, StreamTicket] = {}

    def mint(self, entity_id: str, *, now: float) -> StreamTicket:
        """CasaSmart runtime component."""
        self._purge(now)
        if len(self._tickets) >= MAX_TICKETS:


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
        """CasaSmart runtime component."""
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
