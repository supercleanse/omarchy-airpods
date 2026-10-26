# Validation

## Initial environment

Validated on Omarchy 4.0.4-1 with AirPods Pro, BlueZ 5.87-2, WirePlumber 0.5.17-1, and PipeWire 1.6.8-1. These are tested versions, not an inferred minimum-version promise. No second machine or other AirPods generation has been tested for this release.

## Physical control evidence

Before the AVRCP fix, single, double, and triple stem presses each produced Linux key code 200 (`KEY_PLAYCD`). After enabling WirePlumber's dummy AVRCP player and stopping the conflicting `mpris-proxy`, the capture showed separate codes 201 (`KEY_PAUSECD`), 163 (`KEY_NEXTSONG`), and 165 (`KEY_PREVIOUSSONG`). The user confirmed all three actions worked in the player.

Those events establish that gesture information reaches Linux correctly. The music player decides whether Previous restarts the current track or selects the previous track.

## Package checks

- The initial automated suite contains 72 tests. Tests use temporary directories and mocked system interfaces. They exercise reversible setup, failure recovery, preservation of previous masks/files, edits after installation, device selection, pairing authorization, and optional system-policy handling.
- `omarchy plugin validate .` checks the plugin manifest and entry points.
- The widget was loaded in the running Omarchy shell. Its status probe detected a paired connected device, and the popup opened without plugin QML load errors. The shared IPC target produces the same per-monitor duplicate-handler warning pattern as other multi-monitor widgets; each instance serializes its own requests and mutations also use a filesystem lock.
- After the packaged setup, the user confirmed both stem gestures and the microphone work.
- Real setup, repeated setup, undo, and final setup were exercised on the original laptop. Repeated setup reported no change. Undo restored all sampled original files and masks byte-for-byte before setup was reapplied.
- The original working settings were preserved; this is not a clean-install hardware test. The generated configuration uses the same settings, with device addresses selected locally.

The GitHub Actions workflow records automated checks for each commit. Live Omarchy, Bluetooth, and hardware checks cannot run on GitHub's generic Ubuntu runner.

## Still needs broader testing

- Clean installation on another Omarchy machine and Bluetooth adapter.
- Microphone/call transitions and music restoration in different applications.
- Reboot and suspend/resume cycles with the packaged configuration.
- Optional agent and confirmation-policy behavior during an actual device-initiated re-pair request.
- Other AirPods generations and coexistence with other Bluetooth headphones.

When reporting a problem, include package versions, AirPods model, the failing action, and the plugin status. Remove Bluetooth addresses and personal device names before posting logs. Do not include pairing databases or keys.
