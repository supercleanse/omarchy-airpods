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

## Audio panel integration (0.2.0)

The separate AirPods icon was removed before migration. The cloned audio widget replaced the original Audio entry at its existing position; every other bar entry retained its order. Disable restored the stock Audio widget in place, and enable replaced it again without adding another icon.

Both Audio and AirPods pages were opened and visually inspected in the running shell. The Audio page exposed the existing output/input devices and app mixer. The AirPods page reported configured controls and the connected device. Actual keyboard events verified Ctrl+Tab switching, Escape dismissal, and defaulting to Audio when opened through the existing `omarchy.audio` IPC. Arrow keys on the AirPods page did not alter either audio volume. No apply/restore or Bluetooth restart was performed for this UI migration.

The backend remains unchanged from 0.1.0, with all 72 tests passing. See NOTICE.md for the stock audio source, included license, and the scoped-API limitation on selected-media highlighting.

## Microphone mute and rule correction (0.2.1)

A reported microphone failure was traced to the selected AirPods input being muted. Unmuting restored speech, confirmed by the user. The inherited Audio master switch also muted the microphone; the plugin now makes the main switch, its keyboard action, and speaker-icon right-click affect output only. A live keyboard test toggled the header twice and verified the microphone remained unmuted.

Runtime logs also exposed a separate generated-rule bug: `actions.update-props` was emitted as a single rule key, which WirePlumber ignored. Rules now use nested `actions = { update-props = {...} }`. Native SPA parsing verifies the resulting object structure. Tests cover colon/underscore node names, optional suffixes, exact device boundaries, tracked upgrades, and rollback to the original baseline. All 76 local tests passed. Installed rules parsed correctly and WirePlumber restarted without the earlier unknown-key/no-actions warnings. The native parser regression is skipped on test runners without `spa-json-dump`; it ran on the tested laptop.

## Bluetooth companion packaging (0.3.0)

The focused `tests/test_bluetooth_setup.py` suite covers temporary-path installation, repeat installation, canonical-backend updates, retained previous-version backups, and independent removal. Ownership checks reject edited or added files, unowned directories, edited backups, symlinks, and group/other-writable package ancestors. Failure injection covers validation, deployment rescan, and disable errors.

Fixture-based shell IPC tests exercise replacing exactly one existing Bluetooth slot, keeping its position/settings and the Audio entry unchanged, repeat enable, stock restoration on removal, and refusal when the stock slot is missing/duplicated or another Bluetooth clone is active. These tests execute the helper and simulate Omarchy's external commands; they do not establish live shell behavior. Where installed, the real `omarchy-plugin-validate` checks staged test bundles.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_bluetooth_setup.py -v
```

The assembled companion was staged under a temporary directory and passed the installed manifest validator. All four UI files were included and its backend matched the canonical source byte-for-byte. The packaging task itself made no live changes; the subsequent desktop checks are recorded below. The updater must be run after root-plugin updates because Omarchy has no nested-companion update hook.


### Live Bluetooth recovery checks

On the original laptop, the companion installed and replaced the existing Bluetooth slot without adding a bar entry or changing the Audio slot. Disable restored stock Bluetooth in place; re-enable restored the companion. The popup and recovery page were visually inspected. Keyboard input started discovery, selected the returned AirPods, and ran recovery successfully. Audio remained connected, and the plugin reported “AirPods connected and configured.”

A live test caught a redundant Connect call timing out while music continued playing. Recovery now checks for a connected matching BlueZ device and its actual PipeWire audio sink before skipping that call; an ACL connection alone is insufficient. The corrected command succeeded against the playing AirPods.

The Find AirPods control is outside device lists and has no device-count or Bluetooth-power visibility condition. The initial empty candidate list and later discovered candidate were both inspected. No deliberate unpair, radio-off, or physical adapter-removal test was performed during this feature validation. The earlier manual pairing recovery succeeded on this laptop; the packaged unpaired-device sequence is covered by mocked tests, not a new destructive hardware reset.

The final local suite passed 114 tests. Both plugin manifests passed Omarchy validation. The final deployed recovery UI completed scan, explicit selection, and recovery using keyboard input. Cross-monitor conflict guards use Omarchy’s supported widget-instance lookup; simultaneous physical interactions on two monitors were not exercised.
