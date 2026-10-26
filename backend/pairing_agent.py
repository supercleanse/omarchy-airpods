#!/usr/bin/env python3
"""Optional BlueZ agent for explicitly selected, already paired/trusted AirPods.

Run with --devices-file /path/to/pairing-devices.json (a JSON list of MAC
strings). Selection is user-owned state; this agent never pairs or trusts a
new device. No device names, addresses, PINs, passkeys, or exception details
are logged. Missing/unreadable/invalid state denies every authorization.

--check validates the complete file and Python D-Bus/GLib dependencies only;
it neither connects to a bus nor registers an agent. It does not check BlueZ
availability or device state. An empty list is valid and authorizes nothing.

Default-agent contention: only one default agent should manage this session.
Another pairing UI can replace this agent; Release triggers exponential
retries (2 to 60 seconds), not immediate reclamation. Successful registration
does not reset the backoff, so repeated displacement cannot create a tight
loop. Disable this optional agent while using another persistent pairing
agent. A user service can restart it after system-bus failure. No installation
or service changes are performed here.
"""

import argparse
import json
from pathlib import Path
import re
import sys

CAPABILITY = "NoInputNoOutput"
AGENT_PATH = "/org/omarchy/AirPodsPairingAgent"
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


class DeviceFileError(ValueError):
    """Safe, deliberately nonspecific state-file failure."""


def read_devices(path):
    """Read an all-or-nothing allowlist; never cache authorization state."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list) or any(
            not isinstance(item, str) or MAC.fullmatch(item) is None
            for item in data
        ):
            raise ValueError
        return frozenset(item.upper() for item in data)
    except (OSError, ValueError, TypeError, RecursionError):
        raise DeviceFileError("Device selection file is missing, unreadable, or invalid.") from None


def authorized(devices_file, address, paired, trusted):
    """Allow only a selected address and affirmative paired/trusted booleans."""
    try:
        selected = read_devices(devices_file)
    except DeviceFileError:
        return False
    return (
        isinstance(address, str)
        and MAC.fullmatch(address) is not None
        and address.upper() in selected
        and paired is True
        and trusted is True
    )


def dependencies():
    """Load optional runtime packages only when actually requested."""
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    return dbus, GLib


class Registration:
    """Single outstanding timer, persistent retries, capped exponential delay."""

    def __init__(self, glib, register, minimum=2, maximum=60):
        self.glib = glib
        self.register = register
        self.delay = minimum
        self.maximum = maximum
        self.timer = None
        self.stopped = False

    def schedule(self):
        if not self.stopped and self.timer is None:
            self.timer = self.glib.timeout_add_seconds(self.delay, self.attempt)
            self.delay = min(self.delay * 2, self.maximum)

    def attempt(self):
        self.timer = None
        if self.stopped:
            return False
        try:
            self.register()
        except Exception:
            # Never expose D-Bus errors: they may contain device identifiers.
            self.schedule()
        return False  # Each GLib source is one-shot.

    def stop(self):
        self.stopped = True
        if self.timer is not None:
            self.glib.source_remove(self.timer)
            self.timer = None


def run_agent(devices_file, dbus, glib):
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = glib.MainLoop()

    class Rejected(dbus.DBusException):
        _dbus_error_name = "org.bluez.Error.Rejected"

    class Agent(dbus.service.Object):
        registered = False

        def manager(self):
            return dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                                  "org.bluez.AgentManager1")

        def register(self):
            manager = self.manager()
            if not self.registered:
                manager.RegisterAgent(AGENT_PATH, CAPABILITY)
                self.registered = True
            manager.RequestDefaultAgent(AGENT_PATH)

        def authorize(self, device):
            try:
                props = dbus.Interface(bus.get_object("org.bluez", device),
                                       "org.freedesktop.DBus.Properties")
                state = props.GetAll("org.bluez.Device1")
                allowed = authorized(devices_file, state.get("Address"),
                                     bool(state.get("Paired", False)),
                                     bool(state.get("Trusted", False)))
            except Exception:
                allowed = False
            if not allowed:
                raise Rejected("Authorization denied")

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            self.registered = False
            retry.schedule()

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            self.authorize(device)

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            self.authorize(device)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            self.authorize(device)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            raise Rejected("PIN entry unsupported")

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            raise Rejected("Passkey entry unsupported")

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            raise Rejected("PIN display unsupported")

        @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            raise Rejected("Passkey display unsupported")

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            pass

    agent = Agent(bus, AGENT_PATH)
    retry = Registration(glib, agent.register)

    def owner_changed(name, old, new):
        agent.registered = False
        retry.schedule()  # Includes disappearance; retry persists while absent.

    receiver = bus.add_signal_receiver(
        owner_changed, "NameOwnerChanged", "org.freedesktop.DBus",
        bus_name="org.freedesktop.DBus", arg0="org.bluez")
    retry.attempt()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        retry.stop()
        receiver.remove()
        if agent.registered:
            try:
                agent.manager().UnregisterAgent(AGENT_PATH)
            except Exception:
                pass
        agent.remove_from_connection()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--devices-file", required=True, type=Path)
    parser.add_argument("--check", action="store_true",
                        help="validate file and dependencies without connecting to D-Bus")
    args = parser.parse_args(argv)
    if args.check:
        try:
            read_devices(args.devices_file)
        except DeviceFileError as error:
            print(str(error), file=sys.stderr)
            return 1
    try:
        dbus, glib = dependencies()
    except Exception:
        print("D-Bus/GLib Python dependencies unavailable.", file=sys.stderr)
        return 1
    if args.check:
        print("Device selection and Python dependencies valid; no bus connection made.")
        return 0
    try:
        run_agent(args.devices_file, dbus, glib)
    except Exception:
        print("Pairing agent stopped: runtime unavailable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
