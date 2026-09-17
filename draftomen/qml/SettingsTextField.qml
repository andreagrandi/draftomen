import QtQuick 2.15
import QtQuick.Controls 2.15

TextField {
    id: control

    // Keep the editable target the same height as the other settings controls.
    implicitWidth: 300
    implicitHeight: Theme.targetHeight
    selectByMouse: true
    activeFocusOnTab: true
    focusPolicy: Qt.StrongFocus
    color: Theme.text
    placeholderTextColor: Theme.textMuted
    font.pixelSize: Theme.textPixelSize(13)
    leftPadding: 10
    rightPadding: 10

    readonly property bool visualFocused: control.activeFocus

    signal committed(string value)

    function commit() {
        control.committed(control.text)
    }

    // Enter and focus loss both commit; a repeated equal value is a no-op upstream.
    onAccepted: control.commit()
    onEditingFinished: control.commit()

    background: Rectangle {
        color: Theme.surfaceHigh
        border.color: control.visualFocused ? Theme.focus : Theme.controlNeutralBorder
        border.width: 1
        radius: Theme.radius
    }
}
