import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root

    property string availableVersion: ""
    property string installedVersion: ""
    property string channel: "website"
    property var returnFocusItem: null
    readonly property string websiteUrl: "https://www.draftomen.com"
    readonly property string storeUrl: "https://apps.microsoft.com/detail/9NPCD3VLZQMX"
    readonly property string releasesUrl: "https://github.com/andreagrandi/draftomen/releases"
    readonly property string message: (
        "Version " + root.availableVersion + " is available. You are using "
        + root.installedVersion + ". " + root.channelInstruction
    )
    readonly property string channelInstruction: {
        if (root.channel === "store")
            return "Get it from the Microsoft Store."
        if (root.channel === "github")
            return "Download the unsigned build from the GitHub releases page."
        return "Download it from the website."
    }

    objectName: "updateDialog"
    parent: Overlay.overlay
    modal: true
    focus: true
    title: "Update available"
    width: Math.min(460, Math.max(300, parent ? parent.width - 32 : 460))
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
        implicitHeight: 52
        color: Theme.surfaceHigh
        border.color: Theme.outline
        border.width: 1

        Label {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            text: root.title
            color: Theme.text
            font.pixelSize: Theme.textPixelSize(18)
            font.bold: true
            verticalAlignment: Text.AlignVCenter
        }
    }

    onClosed: {
        const opener = root.returnFocusItem
        root.returnFocusItem = null
        if (opener && opener.visible && opener.enabled)
            opener.forceActiveFocus()
    }

    contentItem: ColumnLayout {
        spacing: 12
        implicitWidth: 360

        Label {
            objectName: "updateDialogMessage"
            Layout.fillWidth: true
            text: root.message
            color: Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }

        DimensionalButton {
            objectName: "updateDialogWebsite"
            visible: root.channel !== "store" && root.channel !== "github"
            Layout.alignment: Qt.AlignHCenter
            text: "Website download page"
            implicitWidth: 200
            Accessible.name: "Open the Draft Omen website download page"
            Accessible.description: root.websiteUrl
            onClicked: Qt.openUrlExternally(root.websiteUrl)
        }

        DimensionalButton {
            objectName: "updateDialogStore"
            visible: root.channel === "store"
            Layout.alignment: Qt.AlignHCenter
            text: "Microsoft Store listing"
            implicitWidth: 200
            Accessible.name: "Open the Draft Omen Microsoft Store listing"
            Accessible.description: root.storeUrl
            onClicked: Qt.openUrlExternally(root.storeUrl)
        }

        DimensionalButton {
            objectName: "updateDialogReleases"
            visible: root.channel === "github"
            Layout.alignment: Qt.AlignHCenter
            text: "GitHub releases"
            implicitWidth: 200
            Accessible.name: "Open the Draft Omen GitHub releases page"
            Accessible.description: root.releasesUrl
            onClicked: Qt.openUrlExternally(root.releasesUrl)
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
            objectName: "updateDialogCloseButton"
            text: "Close"
            accented: false
            implicitWidth: 96
            Accessible.name: "Close Update available dialog"
            onClicked: root.close()
        }
    }
}
