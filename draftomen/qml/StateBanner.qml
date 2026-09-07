import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root

    required property var sessionState

    readonly property bool hasError: sessionState.errors && sessionState.errors.length > 0
    readonly property var setProfile: sessionState.set_profile
    readonly property var ratings: sessionState.ratings
    readonly property var progress: sessionState.progress
    readonly property bool hasProfileStatus: setProfile
        && (setProfile.phase === "loading" || setProfile.phase === "failed")
    readonly property bool hasProgress: progress !== null
        && progress !== undefined
        && progress.operation !== "ratings"
    readonly property bool determinateProgress: hasProgress
        && progress !== null
        && progress !== undefined
        && progress.completed !== null
        && progress.completed !== undefined
        && progress.total !== null
        && progress.total !== undefined
        && progress.total > 0
    readonly property bool hasWarning: ratings && (
        ratings.phase === "unavailable"
            || ratings.phase === "missing"
            || ratings.phase === "failed"
    )
    readonly property bool shown: hasError || hasProfileStatus || hasProgress || hasWarning
    readonly property var activeError: hasError ? sessionState.errors[0] : null
    readonly property string bannerTitle: {
        if (root.activeError)
            return root.activeError.recoverable ? "Recoverable error" : "Application error"
        if (root.hasProfileStatus)
            return root.setProfile.phase === "loading"
                ? "Refreshing hosted profile"
                : "Hosted profile unavailable"
        if (root.hasProgress)
            return "Working"
        if (root.hasWarning)
            return "Ratings unavailable"
        return ""
    }
    readonly property string bannerMessage: {
        if (root.activeError)
            return root.activeError.message
        if (root.hasProfileStatus)
            return root.setProfile.message
        if (root.hasProgress && root.progress !== null
                && root.progress !== undefined)
            return root.progress.message
        if (root.hasWarning)
            return root.ratings.message
        return ""
    }
    readonly property color bannerColor: {
        if (root.hasError)
            return Theme.errorDark
        if (root.hasWarning || root.hasProfileStatus || root.hasProgress)
            return Theme.warningDark
        return Theme.surfaceHigh
    }
    readonly property color bannerBorderColor: {
        if (root.hasError)
            return Theme.error
        if (root.hasWarning || root.hasProfileStatus || root.hasProgress)
            return Theme.warning
        return Theme.outline
    }

    visible: shown
    implicitHeight: shown ? content.implicitHeight + 20 : 0
    color: root.bannerColor
    border.color: root.bannerBorderColor
    border.width: 1
    radius: Theme.radius

    RowLayout {
        id: content
        anchors.fill: parent
        anchors.margins: 10
        spacing: 12

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 3

            Label {
                Layout.fillWidth: true
                text: root.bannerTitle
                color: Theme.text
                font.bold: true
            }
            ProgressBar {
                id: progressBar
                objectName: "stateProgressBar"
                Layout.fillWidth: true
                visible: root.hasProgress
                from: 0
                to: root.determinateProgress && root.progress
                    ? root.progress.total
                    : 1
                value: root.determinateProgress && root.progress
                    && root.progress.completed !== null
                    && root.progress.completed !== undefined
                    ? Math.max(
                        0,
                        Math.min(
                            root.progress.completed,
                            root.progress.total
                        )
                    )
                    : 0
                indeterminate: root.hasProgress && !root.determinateProgress
            }


            Label {
                Layout.fillWidth: true
                text: root.bannerMessage
                color: Theme.textMuted
                wrapMode: Text.WordWrap
            }

            Label {
                Layout.fillWidth: true
                visible: root.hasError
                    && root.sessionState.ratings.phase === "failed"
                text: root.sessionState.ratings.message
                color: Theme.textMuted
                wrapMode: Text.WordWrap
            }

        }

        DimensionalButton {
            id: ratingsDownloadButton
            visible: root.hasWarning
                && root.ratings.set_code !== null
                && root.ratings.set_code !== undefined
            text: "Refresh hosted ratings"
            Accessible.name: "Refresh hosted ratings"
            objectName: "ratingsDownloadButton"
            onClicked: {
                ratingsDownloadDialog.returnFocusItem = ratingsDownloadButton
                ratingsDownloadDialog.open()
            }
        }

        DimensionalButton {
            objectName: "sessionErrorRetryButton"
            visible: root.activeError && root.activeError.recoverable
            text: "Retry"
            Accessible.name: "Retry failed operation"
            Accessible.description: "Retries the published recoverable error."
            onClicked: {
                const error = root.activeError
                if (error)
                    sessionProvider.retryError(error.error_id)
            }
        }

        DimensionalButton {
            objectName: "sessionErrorDismissButton"
            visible: root.activeError
            text: "Dismiss"
            accented: false
            Accessible.name: "Dismiss error"
            Accessible.description: "Dismisses the published error."
            onClicked: {
                const error = root.activeError
                if (error)
                    sessionProvider.dismissError(error.error_id)
            }
        }
    }

    Dialog {
        id: ratingsDownloadDialog
        objectName: "ratingsDownloadDialog"
        property var returnFocusItem: null
        implicitWidth: 400
        modal: true
        focus: true
        parent: Overlay.overlay
        title: "Refresh hosted ratings?"
        onClosed: {
            if (returnFocusItem)
                returnFocusItem.forceActiveFocus()
        }

        Label {
            width: 360
            text: "Check the hosted 17Lands profile for "
                + root.ratings.set_code
                + "? Cached ratings or deterministic fallback remain available while it refreshes."
            color: Theme.text
            wrapMode: Text.WordWrap
        }

        footer: DialogButtonBox {
            DimensionalButton {
                objectName: "ratingsDownloadCancelButton"
                text: "Not now"
                accented: false
                Accessible.name: "Cancel ratings download"
                onClicked: ratingsDownloadDialog.close()
            }

            DimensionalButton {
                objectName: "ratingsDownloadConfirmButton"
                text: "Refresh hosted ratings"
                Accessible.name: "Confirm hosted ratings refresh"
                onClicked: {
                    sessionProvider.requestRatings()
                    ratingsDownloadDialog.close()
                }
            }
        }
    }
}

