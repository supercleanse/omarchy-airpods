import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons as Commons

Controls.ScrollView {
  id: root
  required property var bar
  property bool expanded: false
  property bool externalBusy: false
  property var actionAllowed: function() { return !root.externalBusy }
  property string operation: ""
  readonly property bool busy: operation !== ""
  property var candidates: []
  property string selectedAddress: ""
  property string pendingAddress: ""
  property string message: ""
  property bool failed: false
  property int elapsed: 0
  readonly property string backendPath: decodeURIComponent(Qt.resolvedUrl("backend/airpods.py").toString().replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  signal closeRequested()
  clip: true
  contentWidth: availableWidth
  implicitHeight: content.implicitHeight
  Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

  function focusFirst() { scanButton.forceActiveFocus() }
  function launch(command) {
    if (busy || !actionAllowed()) return
    if (command !== "discover" && !selectedAddress) return
    if (command === "apply" && (!pendingAddress || selectedAddress !== pendingAddress)) return
    operation = command
    elapsed = 0
    failed = false
    message = ""
    var args = ["python3", backendPath, command]
    if (command !== "discover") args = args.concat(["--device", command === "apply" ? pendingAddress : selectedAddress])
    if (command === "apply") args.push("--preserve-known")
    worker.command = args
    worker.running = true
  }
  function finish(code, status) {
    if (!busy) return
    var completed = operation
    var data
    try {
      data = JSON.parse(output.text)
      if (!data || typeof data !== "object" || !Array.isArray(data.devices)) throw new Error("Invalid response")
      failed = code !== 0 || status !== 0
      message = String(data.message || (failed ? "Operation failed. Try again." : "Done."))
      if (Array.isArray(data.issues) && data.issues.length) message += "\n" + data.issues.join("\n")
      if (completed === "discover" && !failed) {
        candidates = data.devices.filter(function(d) { return d && typeof d.address === "string" })
        selectedAddress = "" // Every scan requires an explicit candidate choice.
        pendingAddress = ""
      }
      // Keep choices after recovery/setup and failures so retry remains possible.
      if (completed === "recover" && !failed)
        pendingAddress = data.configuration_pending === true ? selectedAddress : ""
      if (completed === "apply" && !failed) pendingAddress = ""
    } catch (e) {
      failed = true
      message = "Could not read the recovery response. Try again."
    }
    operation = ""
  }

  Process {
    id: worker
    stdout: StdioCollector { id: output; waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code, status) { root.finish(code, status) }
    onRunningChanged: if (!running && root.busy) Qt.callLater(function() {
      if (!worker.running && root.busy) root.finish(-1, 1)
    })
  }
  Timer {
    interval: 1000
    repeat: true
    running: root.busy
    onTriggered: root.elapsed += 1
  }

  ColumnLayout {
    id: content
    width: root.availableWidth
    spacing: Commons.Style.space(10)
    LabelText {
      Layout.fillWidth: true
      text: "Put both AirPods in the case and open the lid. Use your model’s pairing control until the light flashes white, then scan. For AirPods Max, hold the noise-control button until the light flashes white."
    }
    LabelText {
      Layout.fillWidth: true
      text: "Scanning may turn Bluetooth on. Results can include remembered devices; they do not confirm proximity."
      color: Qt.alpha(root.foreground, 0.7)
    }
    LabelText {
      Layout.fillWidth: true
      visible: root.externalBusy
      text: "Waiting for the current Bluetooth device action to finish…"
    }
    ActionButton {
      id: scanButton
      Layout.fillWidth: true
      text: root.operation === "discover" ? "Scanning… " + root.elapsed + "s" : "Scan for AirPods"
      enabled: !root.busy && !root.externalBusy
      onClicked: root.launch("discover")
    }
    LabelText {
      Layout.fillWidth: true
      visible: root.busy && root.operation !== "discover"
      text: (root.operation === "recover" ? "Recovering… " : "Applying setup… ") + root.elapsed + "s"
    }
    Repeater {
      model: root.candidates
      ActionButton {
        required property var modelData
        Layout.fillWidth: true
        text: (root.selectedAddress === modelData.address ? "✓ " : "") + String(modelData.name || "AirPods") + " · " + modelData.address
        enabled: !root.busy && !root.externalBusy
        Accessible.name: "Select " + String(modelData.name || "AirPods") + " " + modelData.address
        onClicked: root.selectedAddress = modelData.address
      }
    }
    ActionButton {
      Layout.fillWidth: true
      visible: root.candidates.length > 0
      text: "Recover selected AirPods"
      enabled: !root.busy && !root.externalBusy && root.selectedAddress !== ""
      onClicked: root.launch("recover")
    }
    LabelText {
      Layout.fillWidth: true
      visible: root.pendingAddress !== ""
      text: "Audio setup is pending. Applying setup may restart audio."
    }
    ActionButton {
      Layout.fillWidth: true
      visible: root.pendingAddress !== ""
      text: "Apply setup for selected device"
      enabled: !root.busy && !root.externalBusy && root.selectedAddress === root.pendingAddress
      onClicked: root.launch("apply")
    }
    LabelText {
      Layout.fillWidth: true
      visible: root.message !== ""
      text: root.message
      color: root.failed ? Commons.Color.urgent : root.foreground
    }
    ActionButton {
      Layout.fillWidth: true
      text: "Back to Bluetooth devices"
      onClicked: root.closeRequested()
    }
  }
  component LabelText: Text {
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: root.foreground
    font.family: root.bar ? root.bar.fontFamily : Commons.Style.font.family
    font.pixelSize: Commons.Style.font.body
  }
  component ActionButton: Controls.Button {
    id: control
    focusPolicy: Qt.StrongFocus
    implicitHeight: Math.max(Commons.Style.space(34), implicitContentHeight + Commons.Style.space(12))
    horizontalPadding: Commons.Style.space(10)
    opacity: enabled ? 1 : 0.5
    Keys.onReturnPressed: if (enabled) clicked()
    Keys.onEnterPressed: if (enabled) clicked()
    onActiveFocusChanged: if (activeFocus) {
      var y = mapToItem(content, 0, 0).y
      var flick = root.contentItem
      if (y < flick.contentY) flick.contentY = y
      else if (y + height > flick.contentY + flick.height)
        flick.contentY = Math.max(0, Math.min(content.height - flick.height, y + height - flick.height))
    }
    contentItem: LabelText {
      text: control.text
      horizontalAlignment: Text.AlignHCenter
      verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
      radius: Commons.Style.cornerRadius
      color: Qt.alpha(root.foreground, control.down ? 0.2 : control.hovered ? 0.12 : 0.06)
      border.width: control.activeFocus ? 2 : 1
      border.color: Qt.alpha(root.foreground, control.activeFocus ? 0.85 : 0.2)
    }
  }
}
