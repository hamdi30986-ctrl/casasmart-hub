"""What the hub knows about each room, in one place.

- ranking: which of a caller's rooms are busiest right now. The Now page and
  the generated room suggestions both use it.
- power: which sensor reports a device's power draw (Energy Saving's wizard).

Import the submodule you need (room_intel.ranking, room_intel.power): this
package exports nothing, so loading the ranking never loads Home Assistant's
entity registry, which only power needs.
"""
