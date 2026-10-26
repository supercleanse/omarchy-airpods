# Omarchy AirPods

AirPods stem controls, music and call reconnection settings, and optional trusted-device pairing recovery for Omarchy.

A native bar widget shows connected AirPods and offers setup, reconnect, and undo. The settings live in your Linux user configuration and work across music players. No Aldus dependency, custom BlueZ build, or background reconnect loop.

## Install

Requires Omarchy 4's Quickshell plugin system, PipeWire, WirePlumber 0.5, BlueZ, Python 3.11+, and a systemd user session. The tested package versions are in [validation](docs/VALIDATION.md). This plugin is not for PulseAudio or Omarchy 3's Waybar setup.

```sh
omarchy plugin add https://github.com/supercleanse/omarchy-airpods --enable
```

Pair your AirPods using Omarchy's Bluetooth panel first. Open the AirPods bar widget and choose **Set up AirPods**. Setup briefly restarts audio. Reconnect the AirPods using the widget if the controls need a fresh connection.

Enabling the widget only reads status. Configuration changes happen when you choose setup. There is no need to run setup on every login.

| Stem gesture | Action |
| --- | --- |
| Single press | Play / pause |
| Double press | Next track |
| Triple press | Previous track or restart the current track, depending on the player |

## What setup changes

- Enables WirePlumber's dummy AVRCP player so AirPods send distinct playback commands.
- Includes both music (A2DP) and call (HFP) services in the reconnect policy for the detected paired AirPods.
- Disables audio-node idle suspension for those AirPods, carrying forward the call-stability setting. This can increase power use while connected.
- Masks the conflicting `mpris-proxy` user service. If the obsolete `bt-media-watch` workaround exists, setup masks it too.
- Saves previous settings and service state for undo. Unrelated devices and configuration files are preserved; the dummy AVRCP player setting itself applies to the Bluetooth monitor globally.

Your device addresses are discovered on your computer and written only to local settings. The repository contains no pairing keys or personal device identifiers. Renamed AirPods can be selected explicitly with the CLI.

## Command line

From the installed plugin directory:

```sh
cd ~/.config/omarchy/plugins/supercleanse.airpods
python3 backend/airpods.py status
python3 backend/airpods.py apply
python3 backend/airpods.py apply --device AA:BB:CC:DD:EE:FF
python3 backend/airpods.py reconnect --device AA:BB:CC:DD:EE:FF
python3 backend/airpods.py restore
```

Commands emit JSON for both the widget and terminal use. `restore` refuses to overwrite settings changed manually after setup. Re-running setup is safe and does not restart audio when nothing changed.

Optional pairing recovery is described in [pairing recovery](docs/PAIRING.md). It is separate from media setup because it registers a Bluetooth agent and may need an administrator to adjust BlueZ's re-pairing policy.

## Remove

Choose **Undo setup** in the widget, or run `python3 backend/airpods.py restore` from its installed directory. If you enabled optional pairing recovery, undo it using the instructions in [pairing recovery](docs/PAIRING.md). Then remove the widget:

```sh
omarchy plugin remove supercleanse.airpods
```

Omarchy's plugin removal deletes the plugin folder; it does not run an uninstall hook. Undo setup **before** removing the plugin. Existing masks and configuration from before installation remain in place after undo.

## Troubleshooting

- **Every gesture only plays or pauses:** check status in the widget. Keep `mpris-proxy` masked, apply setup, then reconnect the AirPods. Do not add a service that restarts the proxy.
- **No AirPods listed:** pair them in the Bluetooth panel. For a renamed pair, use `apply --device` with its Bluetooth address.
- **Music works but calls do not:** check the application's microphone selection and the audio profile. Call support varies with the Bluetooth adapter, firmware, and application; the reconnect rules are not a guarantee of call quality.
- **Status reports configuration was edited:** inspect the local file and saved backup before changing anything. Undo will not discard those edits.

See [validation](docs/VALIDATION.md) for what has actually been tested and what still needs testing on other machines.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 tests/check_manifest.py
omarchy plugin validate .
```

Tests use temporary directories and mocked Bluetooth/service calls. They do not alter your desktop. See [validation](docs/VALIDATION.md) for runtime checks.

## Credits

The AVRCP setting and the warning against combining it with `mpris-proxy` are documented by [LibrePods](https://github.com/kavishdevar/librepods/blob/main/linux/README.md#media-controls-playpauseskip-not-working). This project packages that setting with connection/call configuration, reversible setup, and an Omarchy interface.

References: [WirePlumber Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html), [PipeWire properties](https://docs.pipewire.org/page_man_pipewire-props_7.html), and [BlueZ configuration](https://github.com/bluez/bluez/blob/master/src/main.conf).

Community plugin; not affiliated with Apple or an official Omarchy component. MIT licensed.
