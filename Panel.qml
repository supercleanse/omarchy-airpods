import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons as Commons
import qs.Ui as Ui

Ui.Panel {
  id: root
  moduleName: "supercleanse.airpods"
  ipcTarget: "supercleanse.airpods"
  manageIpc: false

  readonly property string backendPath: decodeURIComponent(Qt.resolvedUrl("backend/airpods.py").toString().replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color muted: Qt.alpha(foreground, 0.65)
  readonly property string fontFamily: bar ? bar.fontFamily : Commons.Style.font.family
  property bool configured: false
  property bool statusKnown: false
  property var devices: []
  property var issues: []
  property string statusMessage: ""
  property string statusError: ""
  property string actionMessage: ""
  property bool actionFailed: false
  // One process and an explicit operation lock serialize actions and reads.
  property string operation: ""
  readonly property bool busy: operation !== ""
  property bool refreshPending: false
  property bool undoArmed: false
  readonly property int connectedCount: devices.filter(function(d) { return d.connected === true }).length

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (busy) { refreshPending = true; return }
    launch("status", [])
  }

  // Backend messages can contain device identifiers; redact them for IPC.
  function diagnosticText(value) {
    var text = String(value || "")
    var identifiers = []
    devices.forEach(function(device) {
      if (device.name) identifiers.push(String(device.name))
      if (device.address) identifiers.push(String(device.address))
    })
    identifiers.sort(function(a, b) { return b.length - a.length })
    identifiers.forEach(function(identifier) {
      text = text.split(identifier).join("[device]")
    })
    return text.replace(/(?:[0-9a-f]{2}:){5}[0-9a-f]{2}/gi, "[address]")
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
    function diagnostics(): string {
      return JSON.stringify({
        statusKnown: root.statusKnown,
        configured: root.configured,
        busy: root.busy,
        issues: root.issues.map(function(issue) { return root.diagnosticText(issue) }),
        statusError: root.diagnosticText(root.statusError),
        actionMessage: root.diagnosticText(root.actionMessage),
        deviceCount: root.devices.length,
        connectedCount: root.connectedCount,
        opened: root.opened,
        // Screen-local logical coordinates; dimensions already include insets.
        card: {
          x: panel.cardOrigin.x,
          y: panel.cardOrigin.y,
          width: panel.contentWidth,
          height: panel.contentHeight
        }
      })
    }
  }

  function launch(command, args) {
    if (busy) return
    operation = command
    worker.command = ["python3", backendPath, command].concat(args)
    worker.running = true
  }

  function act(command, args) {
    if (busy) return
    undoArmed = false
    actionMessage = ""
    actionFailed = false
    launch(command, args || [])
  }

  function finish(exitCode, exitStatus) {
    var completed = operation
    if (!completed) return
    var data = null
    var error = ""
    try {
      data = JSON.parse(output.text)
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid response")
    } catch (e) {
      error = "Invalid backend response."
    }
    var failed = exitCode !== 0 || exitStatus !== 0 || error !== ""
    var message = data && typeof data.message === "string" ? data.message : ""
    var problems = data && Array.isArray(data.issues) ? data.issues.map(String) : []
    if (failed && !message) message = String(errors.text || error || "Backend failed (exit " + exitCode + ").").trim()

    if (completed === "status") {
      if (!failed && (typeof data.configured !== "boolean" || !Array.isArray(data.devices) || !Array.isArray(data.issues))) {
        failed = true
        message = "Invalid backend status."
      }
      statusKnown = !failed
      statusError = failed ? [message].concat(problems).join("\n") : ""
      if (!failed) {
        configured = data.configured
        devices = data.devices.filter(function(d) { return d && d.paired === true })
        issues = problems
        statusMessage = message
      }
    } else {
      actionFailed = failed
      actionMessage = [message || (failed ? "Action failed." : "Done.")].concat(problems).join("\n")
      refreshPending = true
    }
    operation = ""
    // Defer reuse until Process has completed its exit/stream notifications.
    if (refreshPending) {
      refreshPending = false
      Qt.callLater(root.refresh)
    }
  }

  onOpenedChanged: {
    undoArmed = false
    if (opened) refresh()
  }
  Component.onCompleted: refresh() // Read-only: enabling never applies configuration.

  Process {
    id: worker
    stdout: StdioCollector { id: output; waitForEnd: true }
    stderr: StdioCollector { id: errors; waitForEnd: true }
    onExited: function(exitCode, exitStatus) { root.finish(exitCode, exitStatus) }
    onRunningChanged: if (!running && root.busy) Qt.callLater(function() {
      // Failed-to-start does not always emit exited.
      if (!worker.running && root.busy) root.finish(-1, 1)
    })
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: if (!root.busy) root.refresh()
  }
  Timer {
    interval: 6000
    running: root.undoArmed
    onTriggered: root.undoArmed = false
  }

  Ui.BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰋋"
    tooltipText: "AirPods · " + (root.statusKnown ? root.connectedCount + " connected" : "Status unavailable")
    active: root.opened
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: "AirPods"
    Keys.onReturnPressed: root.toggle()
    Keys.onEnterPressed: root.toggle()
    Keys.onSpacePressed: root.toggle()
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton) root.refresh()
      else root.toggle()
    }
  }

  Ui.KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: refreshButton
    contentWidth: fittedContentWidth(Commons.Style.space(350))
    contentHeight: fittedContentHeight(content.implicitHeight, Commons.Style.space(560))

    ScrollView {
      id: scroll
      anchors.fill: parent
      clip: true
      contentWidth: availableWidth
      Keys.onEscapePressed: root.close()

      ColumnLayout {
        id: content
        width: scroll.availableWidth
        spacing: Commons.Style.space(12)

        RowLayout {
          Layout.fillWidth: true
          LabelText {
            text: "AirPods"
            font.pixelSize: Commons.Style.font.heading
            font.bold: true
            Layout.fillWidth: true
          }
          ActionButton {
            id: refreshButton
            text: root.operation === "status" ? "Refreshing…" : "Refresh"
            enabled: !root.busy
            onClicked: root.refresh()
          }
        }
        LabelText {
          Layout.fillWidth: true
          text: !root.statusKnown ? "Status unavailable" : (root.configured ? "Set up · " : "Not set up · ") + root.connectedCount + " connected"
          color: root.muted
        }
        LabelText {
          Layout.fillWidth: true
          visible: root.statusKnown && root.devices.length === 0
          text: "No paired AirPods. Pair in Bluetooth settings."
          color: root.muted
        }
        Repeater {
          model: root.devices
          delegate: RowLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: Commons.Style.space(10)
            ColumnLayout {
              Layout.fillWidth: true
              spacing: Commons.Style.space(3)
              LabelText {
                Layout.fillWidth: true
                text: String(modelData.name || "AirPods")
                font.bold: true
              }
              LabelText {
                Layout.fillWidth: true
                text: root.statusKnown ? (modelData.connected ? "Connected" : "Disconnected") : "Status unavailable"
                color: root.muted
                font.pixelSize: Commons.Style.font.caption
              }
            }
            ActionButton {
              text: "Reconnect"
              visible: modelData.connected === true
              enabled: root.statusKnown && !root.busy && !!modelData.address
              Accessible.name: "Reconnect " + String(modelData.name || "AirPods")
              onClicked: root.act("reconnect", ["--device", String(modelData.address)])
            }
          }
        }
        Rectangle {
          Layout.fillWidth: true
          implicitHeight: 1
          color: Qt.alpha(root.foreground, 0.15)
        }
        ActionButton {
          Layout.fillWidth: true
          text: root.operation === "apply" ? "Setting up…" : "Set up AirPods"
          enabled: root.statusKnown && root.devices.length > 0 && !root.busy
          onClicked: root.act("apply")
        }
        LabelText {
          Layout.fillWidth: true
          text: "Audio restarts during setup."
          color: root.muted
          font.pixelSize: Commons.Style.font.caption
        }
        ActionButton {
          Layout.fillWidth: true
          text: root.operation === "restore" ? "Undoing…" : root.undoArmed ? "Confirm undo" : "Undo plugin setup"
          // Restore must remain available after a partially failed setup.
          enabled: !root.busy
          onClicked: {
            if (root.undoArmed) root.act("restore")
            else root.undoArmed = true
          }
        }
        LabelText {
          Layout.fillWidth: true
          visible: root.actionMessage !== ""
          text: root.actionMessage
          color: root.actionFailed ? Commons.Color.urgent : root.foreground
        }
        LabelText {
          Layout.fillWidth: true
          visible: root.statusError !== ""
          text: root.statusError
          color: Commons.Color.urgent
        }
        Repeater {
          model: root.issues
          LabelText {
            required property string modelData
            Layout.fillWidth: true
            text: modelData
            color: Commons.Color.urgent
          }
        }
        LabelText {
          Layout.fillWidth: true
          visible: root.statusMessage !== "" && root.statusMessage !== root.actionMessage
          text: root.statusMessage
          color: root.muted
        }
      }
    }
  }

  component LabelText: Text {
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Commons.Style.font.body
  }

  component ActionButton: Button {
    id: control
    focusPolicy: Qt.StrongFocus
    implicitHeight: Math.max(Commons.Style.space(32), implicitContentHeight + Commons.Style.space(12))
    horizontalPadding: Commons.Style.space(10)
    opacity: enabled ? 1 : 0.45
    Keys.onReturnPressed: if (enabled) clicked()
    Keys.onEnterPressed: if (enabled) clicked()
    Keys.onEscapePressed: root.close()
    onActiveFocusChanged: if (activeFocus) {
      var point = mapToItem(content, 0, 0)
      var flick = scroll.contentItem
      if (point.y < flick.contentY) flick.contentY = point.y
      else if (point.y + height > flick.contentY + flick.height)
        flick.contentY = Math.min(Math.max(0, content.height - flick.height), point.y + height - flick.height)
    }
    contentItem: LabelText {
      text: control.text
      horizontalAlignment: Text.AlignHCenter
      verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
      radius: Commons.Style.cornerRadius
      color: Qt.alpha(root.foreground, control.down ? 0.20 : control.hovered ? 0.12 : 0.06)
      border.width: control.activeFocus ? 2 : 1
      border.color: Qt.alpha(root.foreground, control.activeFocus ? 0.85 : 0.20)
    }
  }
}
