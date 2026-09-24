import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

FocusScope {
    id: root

    required property var recommendation
    required property bool selected
    required property bool wide
    required property bool secondaryStats
    signal chosen(int grpId)

    readonly property bool recommended: recommendation.rank === 1
    readonly property bool hasRelationshipAdvice:
        Boolean(recommendation.relationship_advice)
    readonly property bool keyboardFocused: activeFocus
    readonly property string colorsText: recommendation.card.colors.length > 0
        ? recommendation.card.colors.join(" · ") : "Colorless"
    readonly property string alsaText: recommendation.average_last_seen_at !== null
        && recommendation.average_last_seen_at !== undefined
        ? Number(recommendation.average_last_seen_at).toFixed(2) : "—"
    readonly property string manaValueText: recommendation.card.mana_value !== null
        && recommendation.card.mana_value !== undefined
        ? Number(recommendation.card.mana_value).toFixed(0) : "—"
    readonly property string winRateText: recommendation.win_rate !== null
        && recommendation.win_rate !== undefined
        ? (Number(recommendation.win_rate) * 100).toFixed(1) + "%" : "—"
    readonly property string stateText: {
        if (keyboardFocused)
            return "Keyboard focused"
        if (selected)
            return "Selected"
        if (recommended)
            return "Recommended"
        return ""
    }
    readonly property color stateColor: {
        if (keyboardFocused)
            return Theme.focus
        if (selected)
            return Theme.warning
        if (recommended)
            return Theme.primary
        return Theme.outline
    }

    implicitHeight: wide
        ? Math.max(84, wideCardDetails.implicitHeight + 16)
        : Math.max(112, narrowCardDetails.implicitHeight + 16)

    Accessible.role: Accessible.ListItem
    Accessible.name: "Rank " + recommendation.rank + ", "
        + recommendation.card.name + ", DO score " + recommendation.score
    Accessible.description: stateText
        + (hasRelationshipAdvice ? ". AI relationship advice available" : "")
        + ". Press Enter or Space to choose this card."
    activeFocusOnTab: true

    Keys.onReturnPressed: chosen(recommendation.card.grp_id)
    Keys.onSpacePressed: chosen(recommendation.card.grp_id)

    Rectangle {
        anchors.fill: parent
        color: {
            if (root.selected)
                return Theme.surfaceHighest
            if (root.recommended)
                return Theme.primaryDark
            return Theme.surface
        }
        border.color: root.stateColor
        border.width: root.keyboardFocused || root.selected || root.recommended ? 2 : 1
        radius: Theme.radius
    }

    Rectangle {
        visible: root.recommended || root.selected || root.keyboardFocused
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 3
        color: root.stateColor
        radius: Theme.radius
    }

    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            root.forceActiveFocus()
            root.chosen(root.recommendation.card.grp_id)
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 14
        anchors.rightMargin: 12
        anchors.topMargin: 8
        anchors.bottomMargin: 8
        spacing: 8

        Label {
            id: rankLabel
            objectName: "recommendationRank"
            Layout.preferredWidth: 30
            Layout.minimumWidth: 30
            Layout.maximumWidth: 30
            text: ("0" + String(root.recommendation.rank)).slice(-2)
            color: root.recommended ? Theme.primary : Theme.textMuted
            font.family: fixedFontFamily
            font.bold: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }

        RowLayout {
            id: cardCell
            objectName: "recommendationCardCell"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.fillHeight: true
            spacing: 8

            Rectangle {
                id: thumbnailFrame
                objectName: "recommendationThumbnailFrame"
                Layout.preferredWidth: 50
                Layout.minimumWidth: 50
                Layout.maximumWidth: 50
                Layout.minimumHeight: 0
                Layout.fillHeight: true
                color: Theme.background
                border.color: Theme.outline
                border.width: 1
                radius: 6
                clip: true

                Image {
                    id: thumbnailImage
                    objectName: "recommendationThumbnailImage"
                    anchors.fill: parent
                    anchors.margins: 2
                    source: root.recommendation.card.image_path || ""
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    visible: status === Image.Ready
                }

                ColumnLayout {
                    id: thumbnailFallback
                    objectName: "recommendationThumbnailFallback"
                    anchors.fill: parent
                    anchors.margins: 4
                    spacing: 3
                    visible: !thumbnailImage.visible

                    Label {
                        objectName: "recommendationThumbnailFallbackLabel"
                        Layout.fillWidth: true
                        text: {
                            if (thumbnailImage.status === Image.Error)
                                return "Image failed to load"
                            if (thumbnailImage.source.toString().length > 0)
                                return "Loading image"
                            return "No image available"
                        }
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(9)
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.WrapAnywhere
                    }
                }
            }

            ColumnLayout {
                id: wideCardDetails
                objectName: "recommendationWideCardDetails"
                visible: root.wide
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 1

                Label {
                    objectName: "recommendationName"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: root.recommendation.card.name
                    color: Theme.text
                    font.pixelSize: Theme.textPixelSize(14)
                    font.bold: root.recommended || root.selected
                    wrapMode: Text.WordWrap
                    elide: Text.ElideNone
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: 8

                    Label {
                        objectName: "recommendationMetadata"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "ALSA " + root.alsaText
                            + " · " + root.recommendation.card.types.join(" · ")
                            + " · MV " + root.manaValueText
                            + " · " + (root.recommendation.source_label
                                || "Source unavailable")
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(11)
                        elide: Text.ElideRight
                    }

                    Label {
                        objectName: "wideRecommendationRelationshipAdviceBadge"
                        visible: root.hasRelationshipAdvice
                        text: "AI ADVICE"
                        color: Theme.primary
                        font.pixelSize: Theme.textPixelSize(10)
                        font.bold: true
                        font.letterSpacing: 0.8
                    }

                    Label {
                        objectName: "recommendationStateBadge"
                        visible: root.stateText.length > 0
                        Layout.alignment: Qt.AlignVCenter
                        Layout.preferredWidth: 102
                        Layout.minimumWidth: 102
                        Layout.maximumWidth: 102
                        text: root.stateText
                        color: root.stateColor
                        font.pixelSize: Theme.textPixelSize(10)
                        font.bold: true
                        font.letterSpacing: 0.8
                        horizontalAlignment: Text.AlignRight
                    }
                }
            }

            ColumnLayout {
                id: narrowCardDetails
                objectName: "recommendationNarrowCardDetails"
                visible: !root.wide
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 4

                RowLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: 8

                    Label {
                        objectName: "recommendationName"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: root.recommendation.card.name
                        color: Theme.text
                        font.pixelSize: Theme.textPixelSize(14)
                        font.bold: root.recommended || root.selected
                        wrapMode: Text.WrapAnywhere
                        elide: Text.ElideNone
                    }

                    Label {
                        objectName: "narrowRecommendationRelationshipAdviceBadge"
                        visible: root.hasRelationshipAdvice
                        text: "AI ADVICE"
                        color: Theme.primary
                        font.pixelSize: Theme.textPixelSize(10)
                        font.bold: true
                        font.letterSpacing: 0.8
                    }

                    Label {
                        objectName: "recommendationStateBadge"
                        visible: root.stateText.length > 0
                        Layout.alignment: Qt.AlignVCenter
                        Layout.preferredWidth: 102
                        Layout.minimumWidth: 102
                        Layout.maximumWidth: 102
                        text: root.stateText
                        color: root.stateColor
                        font.pixelSize: Theme.textPixelSize(10)
                        font.bold: true
                        font.letterSpacing: 0.8
                        horizontalAlignment: Text.AlignRight
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12

                    Label {
                        Layout.fillWidth: true
                        text: root.colorsText
                        color: root.recommendation.card.colors.length > 0
                            ? Theme.colorForMana(root.recommendation.card.colors[0])
                            : Theme.textMuted
                        elide: Text.ElideRight
                    }

                    Label {
                        text: root.recommendation.color_fit || "Open"
                        color: root.recommendation.color_fit === "Off color"
                            ? Theme.warning : Theme.textMuted
                        horizontalAlignment: Text.AlignRight
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12

                    Label {
                        text: "DO " + root.recommendation.score
                        color: root.recommended ? Theme.primary : Theme.text
                        font.family: fixedFontFamily
                        font.pixelSize: Theme.textPixelSize(12)
                        font.bold: true
                    }

                    Label {
                        text: "17L " + root.winRateText
                        color: Theme.text
                        font.family: fixedFontFamily
                        font.pixelSize: Theme.textPixelSize(12)
                    }

                    Label {
                        text: "Grade " + (root.recommendation.letter_grade || "—")
                        color: Theme.warning
                        font.pixelSize: Theme.textPixelSize(12)
                        font.bold: true
                    }

                    Label {
                        visible: root.secondaryStats
                        Layout.fillWidth: true
                        text: "ALSA " + root.alsaText
                            + " · MV " + root.manaValueText
                            + " · " + (root.recommendation.source_label
                                || "Source unavailable")
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(11)
                        horizontalAlignment: Text.AlignRight
                        elide: Text.ElideRight
                    }
                }
            }
        }

        Label {
            objectName: "recommendationColors"
            visible: root.wide
            Layout.preferredWidth: 70
            Layout.minimumWidth: 70
            Layout.maximumWidth: 70
            text: root.colorsText
            color: root.recommendation.card.colors.length > 0
                ? Theme.colorForMana(root.recommendation.card.colors[0])
                : Theme.textMuted
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        Label {
            objectName: "recommendationScore"
            visible: root.wide
            Layout.preferredWidth: 58
            Layout.minimumWidth: 58
            Layout.maximumWidth: 58
            text: root.recommendation.score
            color: root.recommended ? Theme.primary : Theme.text
            font.family: fixedFontFamily
            font.bold: true
            horizontalAlignment: Text.AlignRight
            verticalAlignment: Text.AlignVCenter
        }

        Label {
            objectName: "recommendationWinRate"
            visible: root.wide
            Layout.preferredWidth: 68
            Layout.minimumWidth: 68
            Layout.maximumWidth: 68
            text: root.winRateText
            color: Theme.text
            font.family: fixedFontFamily
            horizontalAlignment: Text.AlignRight
            verticalAlignment: Text.AlignVCenter
        }

        Label {
            objectName: "recommendationGrade"
            visible: root.wide
            Layout.preferredWidth: 44
            Layout.minimumWidth: 44
            Layout.maximumWidth: 44
            text: root.recommendation.letter_grade || "—"
            color: Theme.warning
            font.bold: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }

        Label {
            objectName: "recommendationFit"
            visible: root.wide
            Layout.preferredWidth: 82
            Layout.minimumWidth: 82
            Layout.maximumWidth: 82
            text: root.recommendation.color_fit || "Open"
            color: root.recommendation.color_fit === "Off color"
                ? Theme.warning : Theme.textMuted
            horizontalAlignment: Text.AlignRight
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
    }
}
