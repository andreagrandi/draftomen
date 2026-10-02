import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root

    objectName: "detailedLogsDialog"
    parent: Overlay.overlay
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    title: "Turn on Detailed Logs"
    width: Math.min(420, Math.max(300, parent ? parent.width - 32 : 420))
    x: parent ? Math.max(16, Math.round((parent.width - width) / 2)) : 16
    y: parent ? Math.max(16, Math.round((parent.height - height) / 2)) : 16
    padding: 16

    Overlay.modal: Rectangle {
        color: "#99000000"
    }

    background: Rectangle {
        color: Theme.surface
        border.color: Theme.outline
        border.width: 1
        radius: Theme.radius
    }

    header: Rectangle {
        objectName: "detailedLogsDialogHeader"
        implicitHeight: 52
        color: Theme.surfaceHigh
        border.color: Theme.outline
        border.width: 1

        Label {
            objectName: "detailedLogsDialogTitle"
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            text: root.title
            color: Theme.text
            font.pixelSize: Theme.textPixelSize(18)
            font.bold: true
            verticalAlignment: Text.AlignVCenter
            Accessible.name: text
        }
    }

    contentItem: ColumnLayout {
        spacing: 12
        implicitWidth: 360

        Label {
            objectName: "detailedLogsDialogBody"
            Layout.fillWidth: true
            text: "Arena's Detailed Logs are turned off, so Draft Omen cannot follow your drafts."
            color: Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }

        Label {
            objectName: "detailedLogsDialogStep1"
            Layout.fillWidth: true
            text: "1. In Arena, open Settings → Account."
            color: Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }

        Label {
            objectName: "detailedLogsDialogStep2"
            Layout.fillWidth: true
            text: "2. Turn on Detailed Logs (Plugin Support)."
            color: Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }

        Label {
            objectName: "detailedLogsDialogStep3"
            Layout.fillWidth: true
            text: "3. Restart Arena."
            color: Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }

        Label {
            objectName: "detailedLogsDialogNote"
            Layout.fillWidth: true
            text: "Draft Omen picks up the change after Arena restarts."
            color: Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }
    }

    footer: DialogButtonBox {
        implicitHeight: 58
        alignment: Qt.AlignRight
        background: Rectangle {
            color: Theme.surfaceHigh
            border.color: Theme.outline
            border.width: 1
        }

        DimensionalButton {
            id: closeButton
            objectName: "detailedLogsDialogCloseButton"
            text: "OK"
            accented: false
            implicitWidth: 96
            activeFocusOnTab: true
            focusPolicy: Qt.StrongFocus
            Accessible.role: Accessible.Button
            Accessible.name: "Close Detailed Logs dialog"
            onClicked: root.close()
        }
    }
}
