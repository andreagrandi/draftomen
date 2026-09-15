import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    id: window
    required property var provider

    width: initialWindowWidth
    height: initialWindowHeight
    minimumWidth: 680
    minimumHeight: 640
    visible: true
    title: applicationTitle
    color: Theme.background
    font.pixelSize: guiPreferences.systemTextScaling
        ? guiPreferences.applicationFontPixelSize
        : Theme.baseFontPixelSize

    property string currentSurface: initialSurface
    readonly property bool narrow: width < Theme.narrowBreakpoint
    readonly property var sessionState: provider.state
    readonly property var displayPreferences: guiPreferences
    property string automaticBuildContext: ""
    property string pendingCompletedDraftBuildContext: ""
    property string previousPublishedDraftContext: ""
    property bool previousDraftCompleted: false
    readonly property string desktopApplicationVersion: applicationVersion

    function observePublishedDraftState() {
        const state = window.sessionState
        const draft = state ? state.draft : null
        const draftContext = (
            draft && draft.account_id && draft.draft_id
                ? draft.account_id + ":" + draft.draft_id
                : ""
        )
        const draftCompleted = Boolean(draft && draft.completed)
        const completedTransition = Boolean(
            draftContext
            && draftContext === window.previousPublishedDraftContext
            && !window.previousDraftCompleted
            && draftCompleted
        )

        window.previousPublishedDraftContext = draftContext
        window.previousDraftCompleted = draftCompleted
        if (completedTransition) {
            window.pendingCompletedDraftBuildContext = draftContext
            window.currentSurface = "build"
        }
        Qt.callLater(window.requestCompletedDraftBuild)
    }

    function requestCompletedDraftBuild() {
        const state = window.sessionState
        const errors = state ? state.errors : null
        if (errors && errors.some(error => error && error.operation === "build")) {
            window.automaticBuildContext = ""
            return
        }

        const progress = state ? state.progress : null
        if (progress && progress.operation === "build")
            return

        if (window.currentSurface !== "build")
            return

        const draft = state ? state.draft : null
        const pool = state ? state.pool : null
        const cardData = state ? state.card_data : null
        if (!draft || !draft.completed || !pool || pool.total_cards <= 0
                || !cardData || cardData.phase !== "ready")
            return

        const context = draft.account_id + ":" + draft.draft_id
        const completedTransitionPending = (
            window.pendingCompletedDraftBuildContext === context
        )
        if (!completedTransitionPending
                && (state.build || window.automaticBuildContext === context))
            return

        window.automaticBuildContext = context
        window.pendingCompletedDraftBuildContext = ""
        window.provider.requestBuild("")
    }

    onCurrentSurfaceChanged: Qt.callLater(window.requestCompletedDraftBuild)
    onSessionStateChanged: window.observePublishedDraftState()
    Component.onCompleted: window.observePublishedDraftState()

    header: AppBar {
        narrow: window.narrow
        sessionState: window.sessionState
        provider: window.provider
        onSettingsRequested: window.currentSurface = "settings"
        onTestDraftRequested: opener => {
            testDraftDialog.returnFocusItem = opener
            testDraftDialog.open()
        }
    }

    footer: StatusStrip {
        sessionState: window.sessionState
        displayPreferences: window.displayPreferences
        applicationVersion: window.desktopApplicationVersion
        narrow: window.narrow
    }

    AboutDialog {
        id: aboutDialog
        applicationVersion: window.desktopApplicationVersion
    }

    PrivacyDialog {
        id: privacyDialog
    }

    TestDraftDialog {
        id: testDraftDialog
        sessionState: window.sessionState
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        NavigationRail {
            Layout.fillHeight: true
            currentSurface: window.currentSurface
            compact: window.narrow
            displayPreferences: window.displayPreferences
            onSurfaceRequested: surface => window.currentSurface = surface
            onAboutRequested: opener => {
                aboutDialog.returnFocusItem = opener
                aboutDialog.open()
            }
            onPrivacyRequested: opener => {
                privacyDialog.returnFocusItem = opener
                privacyDialog.open()
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: window.narrow ? 12 : 20
            currentIndex: {
                if (window.currentSurface === "build") return 1
                if (window.currentSurface === "backtest") return 2
                if (window.currentSurface === "settings") return 3
                return 0
            }

            LiveDraftView {
                sessionState: window.sessionState
                recommendationModel: window.provider.recommendationsModel
                narrow: window.narrow
                displayPreferences: window.displayPreferences
            }

            BuildView {
                sessionState: window.sessionState
                narrow: window.narrow
                displayPreferences: window.displayPreferences
            }

            BacktestView {
                sessionState: window.sessionState
                narrow: window.narrow
                displayPreferences: window.displayPreferences
            }

            SettingsView {
                sessionState: window.sessionState
                narrow: window.narrow
                displayPreferences: window.displayPreferences
            }
        }
    }
}

