# Third-party notices

The audio panel and audio model are derived from Omarchy's built-in audio plugin, shipped with Omarchy 4.0.4-1. They are distributed under the MIT license, copyright David Heinemeier Hansson. The full notice is included in [licenses/OMARCHY-MIT.txt](licenses/OMARCHY-MIT.txt).

Source: https://github.com/omacom/omarchy/tree/quattro/shell/plugins/panels/audio

The local base files used for this release had these SHA-256 digests:

- `Panel.qml`: `831efaa29097d06abed31480a9e54208552e3f4e529a5c4ecca9255b12c5a7b1`
- `Model.js`: `9e15d94b2076b039418339ea4e5f4bd305d80d8129a53fd6d3dfb67d1817e4b0`

This plugin adds an Audio / AirPods selector and the AirPods setup page. The stock audio code is included in this repository rather than patched under `/usr/share/omarchy`. The supported clone API does not expose the shell’s selected-media-player service to this widget. Audio sliders, device selection, stream names, and mixer controls remain available; the selected-player highlight in the mixer is unavailable.

Future Omarchy audio-panel changes need to be reviewed and incorporated into plugin updates; the copied panel does not automatically track them.

All original plugin code is covered by the repository's root MIT license.
