#!/usr/bin/env python3
"""Publish the CasaSmart hub's real handshake identity through macOS Bonjour."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import signal
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

_LOGGER = logging.getLogger("casasmart.mdns")
_SERVICE_TYPE = "_casasmart._tcp"


@dataclass(frozen=True)
class Advertisement:
    instance: str
    hostname: str
    address: str
    port: int
    fingerprint: str
    api_version: int
    hub_name: str


def read_handshake(url: str, timeout: float = 5.0) -> dict[str, object]:
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if parsed.scheme != "https" or not is_loopback:
        raise ValueError("handshake URL must be loopback HTTPS")
    context = ssl._create_unverified_context()  # noqa: SLF001 - local identity cert
    with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
        if response.status != 200:
            raise ValueError(f"handshake returned HTTP {response.status}")
        payload = json.loads(response.read(256 * 1024))
    if not isinstance(payload, dict):
        raise ValueError("handshake must be a JSON object")
    return payload


def infer_lan_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    parsed = ipaddress.ip_address(address)
    if not (parsed.is_private or parsed.is_link_local):
        raise ValueError(f"refusing to advertise non-LAN address {address}")
    return address


def advertisement_from_handshake(
    payload: dict[str, object],
    *,
    address: str,
    advertised_port: int,
    hub_name: str,
) -> Advertisement:
    tls = payload.get("tls")
    if not isinstance(tls, dict):
        raise ValueError("handshake has no TLS identity")
    fingerprint = tls.get("identity_fingerprint_sha256")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("handshake TLS fingerprint is invalid")
    try:
        int(fingerprint, 16)
    except ValueError as err:
        raise ValueError("handshake TLS fingerprint is invalid") from err
    api_version = payload.get("api_version")
    if not isinstance(api_version, int) or api_version <= 0:
        raise ValueError("handshake API version is invalid")
    ipaddress.ip_address(address)
    if advertised_port <= 0 or advertised_port > 65535:
        raise ValueError("advertised port is invalid")
    short = fingerprint[:8].lower()
    clean_name = hub_name.strip() or "CasaSmart Hub"
    return Advertisement(
        instance=f"{clean_name} ({short})"[:63],
        hostname=f"casasmart-{short}.local.",
        address=address,
        port=advertised_port,
        fingerprint=fingerprint.lower(),
        api_version=api_version,
        hub_name=clean_name[:63],
    )


def dns_sd_command(advertisement: Advertisement, executable: str) -> list[str]:
    return [
        executable,
        "-P",
        advertisement.instance,
        _SERVICE_TYPE,
        "local.",
        str(advertisement.port),
        advertisement.hostname,
        advertisement.address,
        f"id={advertisement.fingerprint}",
        f"api={advertisement.api_version}",
        "v=1",
        f"name={advertisement.hub_name}",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--handshake",
        default="https://127.0.0.1:18443/api/casasmart/handshake",
    )
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--address")
    parser.add_argument("--name", default="CasaSmart Hub")
    parser.add_argument("--refresh", type=float, default=30.0)
    parser.add_argument("--dns-sd", default="/usr/bin/dns-sd")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopping = False

    def _stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    process: subprocess.Popen[bytes] | None = None
    current: Advertisement | None = None
    while not stopping:
        try:
            payload = read_handshake(args.handshake)
            candidate = advertisement_from_handshake(
                payload,
                address=args.address or infer_lan_address(),
                advertised_port=args.port,
                hub_name=args.name,
            )
            if candidate != current or process is None or process.poll() is not None:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                process = subprocess.Popen(
                    dns_sd_command(candidate, args.dns_sd),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                current = candidate
                _LOGGER.info(
                    "advertising %s at %s:%d",
                    candidate.instance,
                    candidate.address,
                    candidate.port,
                )
        except Exception as err:  # Keep the last valid advertisement alive.
            _LOGGER.warning("advertisement refresh failed: %s", err)
        deadline = time.monotonic() + max(1.0, args.refresh)
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
