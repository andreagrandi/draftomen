pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root

    required property var sessionState
    required property var provider
    required property bool narrow
    signal settingsRequested()
    signal testDraftRequested(var opener)

    readonly property var testDraft: root.sessionState.test_draft || null
    readonly property var moxgate: root.sessionState.moxgate || null
    readonly property bool moxgateRunning: root.moxgate !== null
        && (root.moxgate.phase === "starting"
            || root.moxgate.phase === "waiting"
            || root.moxgate.phase === "receiving")
    readonly property bool testDraftRunning: root.testDraft !== null
        && root.testDraft.active === true
    readonly property var sourceOptions: {
        const options = [{ key: "arena", label: "Arena" }]
        if (root.testDraft !== null && root.testDraft.enabled === true)
            options.push({ key: "test-draft", label: "Mocked Draft" })
        if (root.moxgate !== null && root.moxgate.enabled === true)
            options.push({ key: "moxgate", label: "Moxgate" })
        return options
    }
    readonly property string activeSource: root.moxgateRunning
        ? "moxgate"
        : root.testDraftRunning ? "test-draft" : "arena"
    readonly property int activeSourceIndex: Math.max(
        0, root.sourceOptions.findIndex(option => option.key === root.activeSource))

    color: Theme.surfaceLow
    implicitHeight: 68

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 20
        anchors.rightMargin: 20
        spacing: 16

        Label {
            objectName: "appBarBrandTitle"
            visible: root.narrow
            text: "DRAFTOMEN"
            color: Theme.primary
            font.pixelSize: Theme.textPixelSize(18)
            font.bold: true
            font.letterSpacing: 1.2
        }

        Rectangle {
            visible: root.narrow
            Layout.preferredWidth: 1
            Layout.preferredHeight: 34
            color: Theme.outline
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 2

            Label {
                text: root.sessionState.status ? root.sessionState.status.message : "Starting Draft Omen."
                color: Theme.text
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            Label {
                text: root.sessionState.draft
                    ? root.sessionState.draft.set_code + " · " + root.sessionState.draft.event_name
                    : "No active draft"
                color: Theme.textMuted
                font.pixelSize: Theme.textPixelSize(11)
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
        }

        DimensionalComboBox {
            id: accountSelector
            objectName: "accountSelector"
            Layout.preferredWidth: 180
            visible: root.moxgateRunning
                || (root.sessionState.accounts
                    && root.sessionState.accounts.length > 0)
            // Moxgate drafts are always saved under the fixed moxgate account.
            enabled: !root.moxgateRunning
            model: root.sessionState.accounts || []
            textRole: "screen_name"
            valueRole: "account_id"
            currentIndex: {
                const activeAccount = root.sessionState.active_account
                if (!activeAccount)
                    return -1
                for (let index = 0; index < model.length; index++) {
                    if (model[index].account_id === activeAccount.account_id)
                        return index
                }
                return -1
            }
            displayText: root.moxgateRunning
                ? "moxgate"
                : root.sessionState.active_account
                ? root.sessionState.active_account.screen_name
                    || root.sessionState.active_account.account_id
                : "Choose Arena account"
            Accessible.name: "Arena account"
            Accessible.description: "Choose the Arena account whose live draft is shown."
            onActivated: root.provider.chooseAccount(currentValue)
        }

        DimensionalComboBox {
            id: scenarioSelector
            visible: root.provider && root.provider.mockMode
            Layout.preferredWidth: 126
            model: root.provider ? root.provider.scenarios : []
            currentIndex: root.provider
                ? Math.max(0, root.provider.scenarios.indexOf(root.provider.scenario))
                : -1
            Accessible.name: "Representative state"
            Accessible.description: "Choose deterministic visual-development data."
            onActivated: root.provider.selectScenario(currentText)
        }

        DimensionalComboBox {
            id: sourceSelector
            objectName: "sourceSelector"
            Layout.preferredWidth: 140
            visible: root.sourceOptions.length > 1
            model: root.sourceOptions
            textRole: "label"
            valueRole: "key"
            currentIndex: root.activeSourceIndex
            Accessible.name: "Draft source"
            Accessible.description: "Choose whether drafts come from the Arena log, a developer Mocked Draft, or the Moxgate browser extension."
            onActivated: index => {
                const key = root.sourceOptions[index].key
                if (key === "test-draft") {
                    // The dialog starts or leaves the draft. Until a draft is
                    // under way the selector keeps showing the current source.
                    sourceSelector.currentIndex = Qt.binding(() => root.activeSourceIndex)
                    root.testDraftRequested(sourceSelector)
                } else if (key === "moxgate") {
                    if (!root.moxgateRunning)
                        root.provider.startMoxgate()
                } else {
                    if (root.moxgateRunning)
                        root.provider.stopMoxgate()
                    if (root.testDraftRunning)
                        root.provider.leaveTestDraft()
                }
            }
        }

        DimensionalButton {
            objectName: "settingsButton"
            text: "Settings"
            Accessible.name: "Open settings"
            Accessible.description: "Open draft guidance, display, and accessibility settings."
            onClicked: root.settingsRequested()
        }
    }

    Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: Theme.outline
    }
}

