# Optional pairing recovery

Media setup does not change Bluetooth pairing policy. Use this optional component only if previously paired AirPods stop reconnecting because BlueZ requests authorization while no pairing dialog is available.

The agent accepts authorization only for locally selected devices that BlueZ still reports as **both paired and trusted**. It rejects other devices and PIN/passkey entry. Pair new devices in Omarchy's Bluetooth panel first. It does not recover a deleted bond or silently pair a new device.

## Find AirPods in Bluetooth

For nearby discovery and explicit connection recovery, install the optional Bluetooth companion using the [README instructions](../README.md#bluetooth-companion-030). Open the existing Bluetooth popup and choose **Find AirPods**, then follow its pairing-mode guidance and select a device explicitly. The action remains available when no AirPods are listed. It does not enable the persistent agent or change system policy merely by opening the popup.

The companion and the agent solve different problems. Find AirPods provides an interactive recovery workflow; the optional agent below handles authorization for selected devices that remain paired and trusted. Installing or removing the companion does not install or remove that agent. Update the companion copy with `backend/bluetooth_setup.py update` after updating the root Audio plugin.

## Enable the user agent

The optional agent needs Arch packages `python-dbus` and `python-gobject`, in addition to the normal plugin requirements. Install missing packages with:

```sh
omarchy pkg add python-dbus python-gobject
```

Then, from the installed plugin directory:

```sh
cd ~/.config/omarchy/plugins/supercleanse.airpods
python3 backend/pairing_setup.py enable
python3 backend/pairing_setup.py status
```

To select a single pair, append `--device AA:BB:CC:DD:EE:FF` to `enable`. Selection is stored locally. The installer copies the agent to user data and enables `omarchy-airpods-pairing.service` for login. It will not run alongside the old `bt-autoagent` service; disable that old agent deliberately before choosing this replacement.

Only one default Bluetooth pairing agent can serve a session. The optional agent requests that role and re-registers after BlueZ restarts. Disable it when using another persistent pairing manager or pairing unrelated devices that need an interactive PIN dialog. Errors are logged without device addresses or pairing secrets:

```sh
journalctl --user -u omarchy-airpods-pairing.service
```

## BlueZ re-pairing policy

BlueZ can reject a re-pair request before it reaches an agent. Check the system policy:

```sh
python3 backend/system_policy.py status
```

If re-pair requests are being rejected, an administrator can explicitly enable confirmation-based repair:

```sh
pkexec /usr/bin/python3 -I "$PWD/backend/system_policy.py" enable
```

Review `backend/system_policy.py` before authorizing it: this optional command runs that local helper as administrator. Python isolated mode prevents imports from neighboring plugin files.

This changes only `JustWorksRepairing` in `/etc/bluetooth/main.conf` to `confirm`, with a private backup under `/var/lib/omarchy-airpods`. It does not restart Bluetooth automatically. Apply it when a Bluetooth interruption is convenient:

```sh
pkexec systemctl restart bluetooth.service
```

This is a system-wide policy. Requests go through the active agent; this plugin's agent limits approval to the selected paired/trusted devices. The original laptop used `JustWorksRepairing=always`; the distributable intentionally uses `confirm` to retain an authorization decision. The ordinary widget setup never runs this administrator step.

## Undo pairing recovery

Before removing the plugin, disable its agent:

```sh
python3 backend/pairing_setup.py disable
```

If you changed the system policy through this plugin, restore it separately:

```sh
pkexec /usr/bin/python3 -I "$PWD/backend/system_policy.py" restore
pkexec systemctl restart bluetooth.service
```

Restore refuses to replace a file manually edited after installation. Inspect that conflict before proceeding. Pre-existing media configuration and service masks are managed separately by `backend/airpods.py restore`.

## Validation limits

Agent authorization, registration recovery, installer failure handling, and system-policy restoration are covered by automated tests. That is not an end-to-end re-pairing test across AirPods firmware and Bluetooth adapters. The initial release does not claim to have reproduced and repaired a lost bond on a second machine.
