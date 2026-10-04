# Docker Desktop LAN pairing

Docker Desktop on macOS cannot publish CasaSmart's TLS port directly while
preserving the LAN client's source address. The hub correctly refuses pairing
when that rewritten address does not look local. It also cannot publish the
container's multicast DNS announcement onto the physical LAN reliably.

Use a loopback-only Docker mapping:

```yaml
services:
  homeassistant:
    ports:
      - "8123:8123"
      - "127.0.0.1:18443:8443"
```

Then install the two launch-agent templates after replacing `__PYTHON__` and
`__REPO_ROOT__` with absolute paths. The TLS relay forwards encrypted bytes; it
does not terminate TLS, inspect pairing codes, or contain a hub identity, and it
refuses clients outside local address ranges. The mDNS publisher reads the active
hub's public handshake and advertises that hub's real fingerprint, so the same
files work with a different hub.

Validate from another LAN device:

```sh
curl -k https://HUB_LAN_IP:8443/api/casasmart/handshake
dns-sd -B _casasmart._tcp local.
```
