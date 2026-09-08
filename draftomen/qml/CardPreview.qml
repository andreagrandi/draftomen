import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root

    property var recommendation: null
    property bool loading: false
    property var imageState: null
    property bool detailedIntel: false
    property bool constrainImageFrameToHeight: false
    // Keep the existing detailed-preview sizing by default; Live Draft's
    // wide preview can opt into a larger bounded image.
    property real detailedImageMaximumWidth: 200
    property real detailedImageWidthRatio: 0.52
    readonly property bool hasPickRationale: Boolean(
        recommendation
            && recommendation.concise_explanation !== undefined
    )

    readonly property bool imageCurrent: Boolean(
        recommendation
            && imageState
            && imageState.grp_id === recommendation.card.grp_id
    )
    readonly property bool imageLoading: Boolean(
        loading || imageCurrent && imageState.phase === "loading"
    )
    readonly property bool imageAvailable: Boolean(
        imageCurrent
            && imageState.image_path
            && imageState.phase === "ready"
    )
    readonly property string colorsText: {
        if (!recommendation)
            return "—"
        if (recommendation.card.colors.length === 0)
            return "Colorless"
        return recommendation.card.colors.join(" · ")
    }
    readonly property string manaValueText: recommendation
        && recommendation.card.mana_value !== null
        && recommendation.card.mana_value !== undefined
        ? Number(recommendation.card.mana_value).toFixed(0) : "—"
    readonly property string alsaText: recommendation
        && recommendation.average_last_seen_at !== null
        && recommendation.average_last_seen_at !== undefined
        ? Number(recommendation.average_last_seen_at).toFixed(2) : "—"
    readonly property string winRateText: recommendation
        && recommendation.win_rate !== null
        && recommendation.win_rate !== undefined
        ? (Number(recommendation.win_rate) * 100).toFixed(1) + "%" : "—"

    color: Theme.surfaceLow
    border.color: Theme.outline
    border.width: 1
    radius: Theme.radius
    Accessible.role: Accessible.Pane
    Accessible.name: {
        if (recommendation) {
            const prefix = root.detailedIntel
                ? "Focused card intel, "
                : "Selected card, "
            return prefix + recommendation.card.name
        }
        return root.detailedIntel ? "Focused card intel" : "Card details"
    }

    readonly property real imageFrameAvailableHeight: Math.max(
        0,
        root.height - Theme.panelPadding * 2
            - previewHeading.implicitHeight
            - (previewDetails.visible
                ? previewDetailsColumn.implicitHeight : 0)
            - previewLayout.rowSpacing * (previewDetails.visible ? 2 : 1)
    )
    readonly property real detailedImageFrameAvailableHeight: Math.max(
        0,
        root.height - Theme.panelPadding * 2
            - previewHeading.implicitHeight
            - previewLayout.rowSpacing * 2
    )
    readonly property real detailedContentWidth: root.width
        - Theme.panelPadding * 2 - previewLayout.columnSpacing
    readonly property real detailedImageFrameWidth: {
        const heightBound = Math.max(
            0,
            root.detailedImageFrameAvailableHeight / 1.4
        )
        const maximumWidth = Math.max(
            0, root.detailedImageMaximumWidth
        )
        const contentBound = Math.max(
            0,
            root.detailedContentWidth
                * Math.max(0, root.detailedImageWidthRatio)
        )
        return Math.min(
            maximumWidth,
            heightBound,
            Math.max(120, contentBound)
        )
    }
    readonly property real imageFrameWidth: {
        if (root.detailedIntel)
            return root.detailedImageFrameWidth
        if (root.constrainImageFrameToHeight) {
            return Math.max(
                0,
                Math.min(
                    260,
                    root.width - Theme.panelPadding * 2,
                    root.imageFrameAvailableHeight / 1.4
                )
            )
        }
        return Math.max(0, Math.min(
            240,
            root.width - Theme.panelPadding * 2
        ))
    }
    readonly property real imageFrameHeight: root.imageFrameWidth * 1.4
    readonly property real previewDetailsSideMargin: {
        if (root.detailedIntel || !root.constrainImageFrameToHeight)
            return 0
        return Math.max(
            0,
            (root.width - Theme.panelPadding * 2 - root.imageFrameWidth) / 2
        )
    }

    GridLayout {
        id: previewLayout
        anchors.fill: parent
        anchors.margins: Theme.panelPadding
        columns: root.detailedIntel ? 2 : 1
        rowSpacing: root.detailedIntel ? 10
            : root.constrainImageFrameToHeight ? 18 : 12
        columnSpacing: 12

        Label {
            id: previewHeading
            objectName: "cardPreviewHeading"
            Layout.columnSpan: root.detailedIntel ? 2 : 1
            text: root.detailedIntel ? "FOCUSED INTEL" : "SELECTED CARD"
            color: root.detailedIntel ? Theme.primary : Theme.textMuted
            font.pixelSize: Theme.textPixelSize(11)
            font.bold: true
            font.letterSpacing: 1.2
        }

        Rectangle {
            id: imageFrame
            objectName: "cardPreviewImageFrame"
            Layout.alignment: root.detailedIntel ? Qt.AlignTop : Qt.AlignHCenter
            Layout.preferredWidth: root.imageFrameWidth
            Layout.preferredHeight: root.imageFrameHeight
            color: Theme.background
            border.color: root.imageLoading ? Theme.warning : Theme.outline
            border.width: 2
            radius: 10
            clip: true

            Image {
                id: cardImage
                objectName: "cardPreviewImage"
                anchors.fill: parent
                source: root.imageAvailable ? root.imageState.image_path : ""
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                visible: root.imageAvailable && status === Image.Ready
            }

            ColumnLayout {
                id: previewFallback
                objectName: "cardPreviewFallback"
                anchors.centerIn: parent
                width: parent.width - 32
                spacing: 10
                visible: !cardImage.visible

                Rectangle {
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredWidth: 48
                    Layout.preferredHeight: 48
                    radius: 24
                    color: root.recommendation
                        && root.recommendation.card.colors.length > 0
                        ? Theme.colorForMana(root.recommendation.card.colors[0])
                        : Theme.surfaceHigh
                    opacity: 0.85
                }

                Label {
                    Layout.fillWidth: true
                    text: {
                        if (root.imageLoading)
                            return "Loading card image"
                        if (root.recommendation)
                            return root.recommendation.card.name
                        return "No card selected"
                    }
                    color: Theme.text
                    font.pixelSize: Theme.textPixelSize(16)
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                }

                Label {
                    Layout.fillWidth: true
                    text: {
                        if (cardImage.status === Image.Error)
                            return "Card image could not be displayed."
                        if (root.imageCurrent)
                            return root.imageState.message
                        return "Card image unavailable"
                    }
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(11)
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                }
            }
        }

        Flickable {
            id: previewDetails
            objectName: "cardPreviewDetails"
            visible: root.recommendation !== null
            Layout.alignment: root.detailedIntel ? Qt.AlignTop : Qt.AlignLeft
            Layout.fillWidth: true
            Layout.leftMargin: root.previewDetailsSideMargin
            Layout.rightMargin: root.previewDetailsSideMargin
            Layout.maximumWidth: root.detailedIntel
                ? Math.max(0, root.detailedContentWidth - root.imageFrameWidth)
                : root.width
            Layout.preferredHeight: {
                if (root.detailedIntel)
                    return root.imageFrameHeight
                if (!root.hasPickRationale)
                    return previewDetailsColumn.implicitHeight
                return Math.min(
                    previewDetailsColumn.implicitHeight,
                    Math.max(
                        0,
                        root.height - Theme.panelPadding * 2
                            - previewHeading.implicitHeight
                            - root.imageFrameHeight
                            - previewLayout.rowSpacing * 2
                    )
                )
            }
            contentWidth: width
            contentHeight: previewDetailsColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            activeFocusOnTab: root.detailedIntel
            Accessible.role: Accessible.Pane
            Accessible.name: "Scrollable card details"
            Accessible.description: "Use Up and Down, Page Up, Page Down, Home, or End to scroll the card details."

            function scrollTo(position) {
                contentY = Math.max(0, Math.min(
                    position, Math.max(0, contentHeight - height)
                ))
            }

            Keys.onPressed: function(event) {
                const pageStep = Math.max(1, height - 24)
                const lineStep = 40
                switch (event.key) {
                case Qt.Key_Up:
                    previewDetails.scrollTo(contentY - lineStep)
                    break
                case Qt.Key_Down:
                    previewDetails.scrollTo(contentY + lineStep)
                    break
                case Qt.Key_PageUp:
                    previewDetails.scrollTo(contentY - pageStep)
                    break
                case Qt.Key_PageDown:
                    previewDetails.scrollTo(contentY + pageStep)
                    break
                case Qt.Key_Home:
                    previewDetails.scrollTo(0)
                    break
                case Qt.Key_End:
                    previewDetails.scrollTo(contentHeight - height)
                    break
                default:
                    return
                }
                event.accepted = true
            }

            ScrollBar.vertical: ScrollBar {
                objectName: "cardPreviewScrollBar"
                policy: ScrollBar.AsNeeded
                width: 8
                Accessible.name: "Card details vertical scrollbar"
            }

            ColumnLayout {
                id: previewDetailsColumn
                width: previewDetails.width
                spacing: 6

                Label {
                    objectName: "cardPreviewName"
                    Layout.fillWidth: true
                    text: root.recommendation ? root.recommendation.card.name : ""
                    color: Theme.text
                    font.pixelSize: Theme.textPixelSize(18)
                    font.bold: true
                    wrapMode: Text.WordWrap
                }

                Label {
                    objectName: "cardPreviewFacts"
                    Layout.fillWidth: true
                    text: {
                        if (!root.recommendation)
                            return ""
                        if (root.detailedIntel)
                            return root.colorsText + "  ·  "
                                + root.recommendation.card.types.join(" · ")
                                + "  ·  MV " + root.manaValueText
                        return root.recommendation.card.types.join(" · ")
                            + "   ·   MV " + root.recommendation.card.mana_value
                    }
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(12)
                    wrapMode: Text.WordWrap
                }

                GridLayout {
                    visible: root.detailedIntel
                    objectName: "cardPreviewScores"
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 4

                    Label { text: "DO Score"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11) }
                    Label {
                        text: root.recommendation ? root.recommendation.score : "—"
                        color: Theme.primary
                        font.family: fixedFontFamily
                        font.pixelSize: Theme.textPixelSize(16)
                        font.bold: true
                    }

                    Label { text: "17L WR"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11) }
                    Label {
                        text: root.winRateText
                        color: Theme.text
                        font.family: fixedFontFamily
                        font.pixelSize: Theme.textPixelSize(15)
                    }

                    Label { text: "Grade"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11) }
                    Label {
                        text: root.recommendation
                            ? root.recommendation.letter_grade || "—" : "—"
                        color: Theme.warning
                        font.pixelSize: Theme.textPixelSize(15)
                        font.bold: true
                    }

                    Label { text: "ALSA"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11) }
                    Label {
                        text: root.alsaText
                        color: Theme.text
                        font.family: fixedFontFamily
                        font.pixelSize: Theme.textPixelSize(15)
                    }

                    Label { text: "Fit"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11) }
                    Label {
                        Layout.fillWidth: true
                        text: root.recommendation
                            ? root.recommendation.color_fit || "Open" : "—"
                        color: Theme.text
                        elide: Text.ElideRight
                    }

                    Label { text: "Source"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11) }
                    Label {
                        Layout.fillWidth: true
                        text: root.recommendation
                            ? root.recommendation.source_label || "Unavailable" : "—"
                        color: Theme.text
                        elide: Text.ElideRight
                    }
                }

                RowLayout {
                    visible: !root.detailedIntel
                    Layout.fillWidth: true

                    Label {
                        text: root.recommendation ? "DO " + root.recommendation.score : ""
                        color: Theme.primary
                        font.family: fixedFontFamily
                        font.bold: true
                    }

                    Label {
                        text: root.recommendation && root.recommendation.win_rate !== null
                            ? "17L " + (root.recommendation.win_rate * 100).toFixed(1) + "%"
                            : "17L —"
                        color: Theme.textMuted
                        font.family: fixedFontFamily
                    }

                    Label {
                        text: root.recommendation
                            ? root.recommendation.letter_grade || "—" : ""
                        color: Theme.warning
                        font.bold: true
                    }
                }

                Label {
                    objectName: "cardPreviewExplanation"
                    Layout.fillWidth: true
                    text: {
                        if (!root.recommendation)
                            return ""
                        if (root.detailedIntel) {
                            return root.recommendation.explanation
                                || "Explanation unavailable."
                        }
                        return root.recommendation.concise_explanation || ""
                    }
                    textFormat: Text.PlainText
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(12)
                    wrapMode: Text.WordWrap
                }
            }
        }

        Item {
            Layout.columnSpan: root.detailedIntel ? 2 : 1
            Layout.fillHeight: true
        }
    }
}

