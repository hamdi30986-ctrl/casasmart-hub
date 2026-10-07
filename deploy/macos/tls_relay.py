#!/usr/bin/env python3
"""Expose a container-only CasaSmart TLS listener on the macOS LAN.

Docker Desktop does not preserve the LAN client's address when it publishes a
container port directly. CasaSmart pairing is intentionally LAN-only, so the
hub rejects that rewritten address. This byte-for-byte TCP relay accepts the
LAN connection on macOS and forwards it through the loopback-only Docker port.
TLS remains end-to-end between the app and the hub.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import signal

_LOGGER = logging.getLogger("casasmart.tls_relay")
_BUFFER_SIZE = 64 * 1024


def is_lan_peer(peer: object) -> bool:
    """Return whether a socket peer belongs to a local-only address range."""
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        return False
    try:
        address = ipaddress.ip_address(peer[0].split("%", 1)[0])
    except ValueError:
        return False
    return address.is_private or address.is_link_local or address.is_loopback


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(_BUFFER_SIZE):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def relay_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    if not is_lan_peer(peer):
        _LOGGER.warning("refusing non-LAN client %s", peer)
        client_writer.close()
        with contextlib.suppress(Exception):
            await client_writer.wait_closed()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_host, upstream_port), timeout=5
        )
    # asyncio.TimeoutError, not the builtin: before Python 3.11 they are
    # different classes, and macOS's own python3 is 3.9.
    except (OSError, asyncio.TimeoutError) as err:
        _LOGGER.warning("upstream unavailable for %s: %s", peer, err)
        client_writer.close()
        with contextlib.suppress(Exception):
            await client_writer.wait_closed()
        return

    await asyncio.gather(
        _pipe(client_reader, upstream_writer),
        _pipe(upstream_reader, client_writer),
    )


async def start_relay(
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
) -> asyncio.AbstractServer:
    async def _accept(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await relay_connection(
            reader,
            writer,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )

    return await asyncio.start_server(
        _accept,
        host=listen_host,
        port=listen_port,
        reuse_address=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8443)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=18443)
    return parser


async def _run(args: argparse.Namespace) -> None:
    server = await start_relay(
        args.listen_host,
        args.listen_port,
        args.upstream_host,
        args.upstream_port,
    )
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    _LOGGER.info(
        "listening on %s; forwarding to %s:%d",
        sockets,
        args.upstream_host,
        args.upstream_port,
    )

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopped.set)
    await stopped.wait()
    server.close()
    await server.wait_closed()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
