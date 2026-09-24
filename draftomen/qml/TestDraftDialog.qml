import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root

    required property var sessionState
    property var returnFocusItem: null
    property string selectedMode: "manual"

    readonly property var testDraft: root.sessionState.test_draft || null
    readonly property bool active: root.testDraft !== null && root.testDraft.active === true
    readonly property bool pending: root.testDraft !== null && root.testDraft.pending === true
    readonly property string phase: root.testDraft ? String(root.testDraft.phase) : "idle"
    readonly property string error: root.testDraft && root.testDraft.error
        ? String(root.testDraft.error) : ""
    readonly property bool bulkFileMissing: root.testDraft !== null
        && root.testDraft.bulk_file_missing === true
    readonly property bool bulkDownloading: root.testDraft !== null
        && root.testDraft.bulk_file_downloading === true
    readonly property int bulkDownloadPercent: {
        const value = root.testDraft ? root.testDraft.bulk_file_download_percent : null
        return value === null || value === undefined ? -1 : Math.round(Number(value))
    }
    readonly property bool cardDataDownloading: root.testDraft !== null
        && root.testDraft.card_data_downloading === true
    readonly property var supportedSets: root.testDraft && root.testDraft.supported_sets
        ? root.testDraft.supported_sets : []
    property string chosenSetCode: ""
    readonly property int selectedSetIndex: {
        const sets = root.supportedSets
        if (sets.length === 0)
            return -1
        const testDraft = root.testDraft
        const defaultCode = testDraft && testDraft.default_set_code
            ? String(testDraft.default_set_code).toLowerCase() : ""
        let defaultIndex = 0
        for (let index = 0; index < sets.length; index++) {
            const code = String(sets[index].code).toLowerCase()
            if (code === root.chosenSetCode)
                return index
            if (code === defaultCode)
                defaultIndex = index
        }
        return defaultIndex
    }
    readonly property var selectedSet: root.selectedSetIndex >= 0
        ? root.supportedSets[root.selectedSetIndex] : null
    readonly property string selectedSetCode: root.selectedSet
        ? String(root.selectedSet.code) : ""
    readonly property string selectedSetLabel: root.selectedSet
        ? String(root.selectedSet.name) + " (" + root.selectedSetCode.toUpperCase() + ")" : ""
    readonly property bool selectedSetReady: root.selectedSet !== null
        && root.selectedSet.card_data_cached === true

    ButtonGroup {
        id: testDraftModeGroup
        exclusive: true
    }

    objectName: "testDraftDialog"
    parent: Overlay.overlay
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    title: "Mocked Draft"
    width: Math.min(460, Math.max(320, parent ? parent.width - 32 : 460))
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
        objectName: "testDraftDialogHeader"
        implicitHeight: 52
        color: Theme.surfaceHigh
        border.color: Theme.outline
        border.width: 1

        Label {
            objectName: "testDraftDialogTitle"
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
            text: "Set"
            color: Theme.textMuted
            Accessible.name: text
        }

        DimensionalComboBox {
            id: setSelector
            objectName: "testDraftSetSelector"
            Layout.fillWidth: true
            enabled: !root.active && !root.pending
            model: root.supportedSets.map(function(set) {
                return String(set.name) + " (" + String(set.code).toUpperCase() + ")"
            })
            currentIndex: root.selectedSetIndex
            // Each published state rebuilds the model, and ComboBox then resets
            // currentIndex to 0, so restore the selected set once the reset lands.
            onModelChanged: Qt.callLater(function() {
                setSelector.currentIndex = root.selectedSetIndex
            })
            onActivated: function(index) {
                root.chosenSetCode = String(root.supportedSets[index].code).toLowerCase()
            }
            Accessible.name: "Mocked Draft set"
            Accessible.description: "Choose the simulated draft set."
        }

        Label {
            text: "Mode"
            color: Theme.textMuted
            Accessible.name: text
        }

        RowLayout {
            spacing: 8

            DimensionalButton {
                objectName: "testDraftManualModeButton"
                text: "Manual"
                checkable: true
                checked: root.selectedMode === "manual"
                accented: checked
                enabled: !root.active && !root.pending
                ButtonGroup.group: testDraftModeGroup
                Accessible.name: "Manual mode"
                Accessible.description: "Confirm every pick yourself in the live drafting view."
                onClicked: root.selectedMode = "manual"
            }

            DimensionalButton {
                objectName: "testDraftAutoModeButton"
                text: "Auto"
                checkable: true
                checked: root.selectedMode === "auto"
                accented: checked
                enabled: !root.active && !root.pending
                ButtonGroup.group: testDraftModeGroup
                Accessible.name: "Auto mode"
                Accessible.description: "Let Draft Omen pick for the whole simulated draft."
                onClicked: root.selectedMode = "auto"
            }
        }

        ProgressBar {
            objectName: "testDraftDownloadProgress"
            Layout.fillWidth: true
            visible: root.bulkDownloading
            from: 0
            to: 100
            value: root.bulkDownloadPercent >= 0 ? root.bulkDownloadPercent : 0
            indeterminate: root.bulkDownloadPercent < 0
            Accessible.name: "Scryfall card data download progress"
        }

        ProgressBar {
            objectName: "testDraftCardDataProgress"
            Layout.fillWidth: true
            visible: root.cardDataDownloading
            indeterminate: true
            Accessible.name: "Set card data download progress"
        }

        Label {
            objectName: "testDraftMessage"
            Layout.fillWidth: true
            text: {
                if (root.error.length > 0)
                    return root.error
                if (root.bulkDownloading) {
                    return root.bulkDownloadPercent >= 0
                        ? "Downloading Scryfall card data… " + root.bulkDownloadPercent + "%"
                        : "Downloading Scryfall card data…"
                }
                if (root.cardDataDownloading)
                    return "Downloading card data for " + root.selectedSetLabel + "…"
                if (root.phase === "starting")
                    return "Starting the simulated draft…"
                if (root.phase === "drafting" && root.active) {
                    return root.selectedMode === "manual"
                        ? "Pick each card in the live drafting view."
                        : "Draft Omen is picking automatically."
                }
                if (root.phase === "completed")
                    return "The simulated draft is complete."
                if (root.bulkFileMissing)
                    return "The Scryfall card data Mocked Draft needs is missing. "
                        + "Download it to continue."
                if (root.selectedSet !== null && !root.selectedSetReady)
                    return "Card data for " + root.selectedSetLabel
                        + " is not downloaded yet. Download it to start."
                return "Choose a set and mode, then start."
            }
            color: root.error.length > 0 ? Theme.error : Theme.text
            wrapMode: Text.WordWrap
            Accessible.name: text
        }

        DimensionalButton {
            objectName: "testDraftDownloadButton"
            Layout.fillWidth: true
            text: "Download Scryfall data"
            accented: false
            visible: root.bulkFileMissing && !root.active
            enabled: !root.pending
            activeFocusOnTab: true
            focusPolicy: Qt.StrongFocus
            Accessible.role: Accessible.Button
            Accessible.name: "Download Scryfall card data"
            Accessible.description: "Download Scryfall's default-cards bulk file into "
                + "the configured Mocked Draft bulk file location."
            onClicked: sessionProvider.downloadTestDraftBulkFile()
        }

        DimensionalButton {
            objectName: "testDraftCardDataDownloadButton"
            Layout.fillWidth: true
            text: "Download set card data"
            accented: false
            visible: root.selectedSet !== null && !root.selectedSetReady && !root.active
            enabled: !root.pending
            activeFocusOnTab: true
            focusPolicy: Qt.StrongFocus
            Accessible.role: Accessible.Button
            Accessible.name: "Download card data for " + root.selectedSetLabel
            Accessible.description: "Download and validate the Draft Omen card data "
                + "this set needs before a Mocked Draft can start."
            onClicked: sessionProvider.downloadTestDraftCardData(root.selectedSetCode)
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
            objectName: "testDraftStartButton"
            text: "Start"
            accented: true
            visible: !root.active
            enabled: !root.pending && root.selectedSetReady
            implicitWidth: 120
            activeFocusOnTab: true
            focusPolicy: Qt.StrongFocus
            Accessible.role: Accessible.Button
            Accessible.name: "Start Mocked Draft"
            onClicked: sessionProvider.startTestDraft(
                root.selectedMode, root.selectedSetCode
            )
        }

        DimensionalButton {
            objectName: "testDraftLeaveButton"
            text: "Leave mocked draft"
            accented: false
            visible: root.active
            enabled: !root.pending
            implicitWidth: 120
            activeFocusOnTab: true
            focusPolicy: Qt.StrongFocus
            Accessible.role: Accessible.Button
            Accessible.name: "Leave Mocked Draft"
            onClicked: sessionProvider.leaveTestDraft()
        }

        DimensionalButton {
            objectName: "testDraftCloseButton"
            text: "Close"
            accented: false
            implicitWidth: 120
            activeFocusOnTab: true
            focusPolicy: Qt.StrongFocus
            Accessible.role: Accessible.Button
            Accessible.name: "Close Mocked Draft dialog"
            onClicked: root.close()
        }
    }
}
