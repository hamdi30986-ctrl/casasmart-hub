"""What the hub knows about each room, in one place.

- ranking: which of a caller's rooms have something on, in order, and the
  off rooms that fill the Now page's empty cards. The Now page and the
  generated room suggestions both use it.
- off_rooms: the all-off tiers for rooms with nothing on (still running,
  just went dark, room order), free of Home Assistant.
- meters: which sensors are power meters and what they read, for the
  ranking; free of Home Assistant.
- power: which sensor reports a device's power draw (Energy Saving's wizard).
- activity: the room activity log's rules (what is an event, its source, how
  long it is kept), free of Home Assistant.
- recorder: logs those events from Home Assistant's state changes into
  storage.RoomActivityTable.
- habits: the room habit model (when each room is usually on), its layers,
  profiles (Ramadan apart from the rest of the year) and settings, free of
  Home Assistant.
- learner: builds that model from the log once a day and keeps it, with
  each finished Ramadan, in storage.
- hijri: when Ramadan is, from the Umm al-Qura calendar, offline.

Import the submodule you need (room_intel.ranking, room_intel.power, ...):
this package exports nothing, so loading the ranking never loads Home
Assistant's entity registry, which only power needs.
"""
