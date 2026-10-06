# Docker Desktop LAN pairing

Docker Desktop on macOS cannot publish CasaSmart's TLS port directly while
preserving the LAN client's source address. The address the hub sees is
synthetic and not stable: after a container restart it can even be an
arbitrary public address. Docker Desktop also cannot publish the container's
multicast DNS announcement onto the physical LAN reliably.

So on Docker Desktop the hub (2.2.0+) does not judge "is this phone on the
LAN?" by address on its TLS port. It detects Docker Desktop (the VM kernel
reports `linuxkit`) and treats its TLS listener as LAN ingress. That is only
safe because of the setup below: the TLS port is published to `127.0.0.1` only,
and the relay admits only clients from local address ranges. Requests that
crossed Cloudflare are never treated as LAN. To override detection, set
`lan_relay_ingress` in the hub config to `"on"` or `"off"` (default `"auto"`).
The hub logs its choice at startup ("LAN relay ingress on ...").

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
