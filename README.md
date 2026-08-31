# CasaSmart Hub

Home Assistant integration for CasaSmart systems.

The hub TLS endpoint must be reachable from client devices on port `8443`, and
`_casasmart._tcp` must be advertised on the LAN. Native Home Assistant hosts do
this through the integration. Docker Desktop for macOS requires the companion
[LAN pairing bridge](deploy/macos/README.md) because its port publishing and
multicast behavior hide the client network from the container.

Proprietary software. © CasaSmart. All rights reserved.
