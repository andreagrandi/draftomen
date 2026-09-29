pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root

    required property var pool
    required property bool narrow

    readonly property bool hasPool: root.pool && root.pool.total_cards > 0
    readonly property var colorDistribution: root.pool
        && root.pool.color_distribution ? root.pool.color_distribution : []
    readonly property var manaCurve: root.pool
        && root.pool.mana_curve ? root.pool.mana_curve : []
    readonly property var recentPicks: root.pool && root.pool.recent_picks
        ? root.pool.recent_picks : []
    readonly property int maxManaCount: {
        let largest = 0
        for (let index = 0; index < root.manaCurve.length; index++)
            largest = Math.max(largest, Number(root.manaCurve[index]))
        return largest
    }

    // Panel height that shows everything down to the mana curve bars and
    // their labels; the recent picks below stay reachable by scrolling.
    readonly property real manaCurveRequiredHeight: root.hasPool
        ? Theme.panelPadding * 2 + poolHeader.implicitHeight
            + poolColumn.spacing + poolContent.implicitHeight
            - recentPicksGallery.implicitHeight - poolContent.spacing
        : 0

    color: Theme.surfaceLow
    border.color: Theme.outline
    border.width: 1
    radius: Theme.radius
    Accessible.role: Accessible.Pane
    Accessible.name: "Draft pool summary"
    Accessible.description: "Scrollable pool details. Use Up and Down, Page Up and Page Down, Home, or End to move through the summary."
    function averageManaValueText() {
        const average = root.pool ? root.pool.average_mana_value : null
        if (average === null || average === undefined)
            return "Average mana value: —"
        return "Average mana value: " + Number(average).toFixed(2)
    }

    function manaLabel(index) {
        return index === 6 ? "6+" : String(index)
    }

    function colorLabel(symbol) {
        if (symbol === "W") return "White"
        if (symbol === "U") return "Blue"
        if (symbol === "B") return "Black"
        if (symbol === "R") return "Red"
        if (symbol === "G") return "Green"
        return "Colorless"
    }

    Flickable {
        id: poolFlickable
        objectName: "poolSummaryFlickable"
        anchors.fill: parent
        anchors.margins: Theme.panelPadding
        contentWidth: width
        contentHeight: poolColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        activeFocusOnTab: true
        Accessible.role: Accessible.Pane
        Accessible.name: "Scrollable pool details"
        Accessible.description: "Use Up and Down, Page Up and Page Down, Home, or End to scroll the pool summary."

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
                poolFlickable.scrollTo(contentY - lineStep)
                break
            case Qt.Key_Down:
                poolFlickable.scrollTo(contentY + lineStep)
                break
            case Qt.Key_PageUp:
                poolFlickable.scrollTo(contentY - pageStep)
                break
            case Qt.Key_PageDown:
                poolFlickable.scrollTo(contentY + pageStep)
                break
            case Qt.Key_Home:
                poolFlickable.scrollTo(0)
                break
            case Qt.Key_End:
                poolFlickable.scrollTo(contentHeight - height)
                break
            default:
                return
            }
            event.accepted = true
        }

        ScrollBar.vertical: ScrollBar {
            objectName: "poolSummaryScrollBar"
            policy: ScrollBar.AsNeeded
            width: 8
            Accessible.name: "Pool summary vertical scrollbar"
        }
        ColumnLayout {
            id: poolColumn
            width: parent.width
            spacing: 12

            RowLayout {
                id: poolHeader
                Layout.fillWidth: true

                Label {
                    Layout.fillWidth: true
                    text: "POOL SUMMARY"
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(11)
                    font.bold: true
                    font.letterSpacing: 1.2
                }

                Label {
                    objectName: "poolCount"
                    text: (root.pool ? root.pool.total_cards : 0)
                        + " / " + (root.pool ? root.pool.target_cards : 0)
                        + " cards"
                    color: Theme.primary
                    font.family: fixedFontFamily
                    font.bold: true
                }
            }

            Rectangle {
                visible: !root.hasPool
                Layout.fillWidth: true
                Layout.preferredHeight: 108
                color: Theme.surface
                radius: Theme.radius

                ColumnLayout {
                    anchors.centerIn: parent
                    width: parent.width - 24
                    spacing: 6

                    Label {
                        Layout.fillWidth: true
                        text: "No drafted cards yet"
                        color: Theme.text
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }

                    Label {
                        Layout.fillWidth: true
                        text: "Your pool will appear after the first pick."
                        color: Theme.textMuted
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                    }
                }
            }

            ColumnLayout {
                id: poolContent
                visible: root.hasPool
                Layout.fillWidth: true
                spacing: 8

                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 6

                    Label { text: "Inferred pair"; color: Theme.textMuted }
                    Label {
                        text: root.pool.inferred_pair || "Open"
                        color: Theme.text
                        font.bold: true
                    }

                    Label { text: "Commitment"; color: Theme.textMuted }
                    Label {
                        text: Math.round(Number(root.pool.commitment || 0) * 100)
                            + "% building"
                        color: Theme.warning
                        font.family: fixedFontFamily
                    }
                }

                Label {
                    text: "COLOR DISTRIBUTION"
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(11)
                    font.bold: true
                    font.letterSpacing: 1
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 5

                    Repeater {
                        model: root.colorDistribution

                        delegate: Rectangle {
                            required property var modelData
                            width: colorName.implicitWidth + colorCount.implicitWidth + 24
                            height: 26
                            radius: Theme.radius
                            color: Theme.surface
                            border.color: Theme.outline

                            Row {
                                anchors.centerIn: parent
                                spacing: 5

                                Rectangle {
                                    width: 8
                                    height: 8
                                    radius: 4
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: Theme.colorForMana(modelData[0])
                                }

                                Label {
                                    id: colorName
                                    text: root.colorLabel(modelData[0])
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.textPixelSize(11)
                                }

                                Label {
                                    id: colorCount
                                    text: String(modelData[1])
                                    color: Theme.text
                                    font.family: fixedFontFamily
                                    font.bold: true
                                }
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true

                    Label {
                        Layout.fillWidth: true
                        text: "MANA CURVE"
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(11)
                        font.bold: true
                        font.letterSpacing: 1
                    }

                    Label {
                        objectName: "poolManaCurveAverage"
                        text: root.averageManaValueText()
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(11)
                        Accessible.name: text
                    }
                }

                RowLayout {
                    objectName: "poolManaCurveChart"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 94
                    spacing: 5

                    Repeater {
                        model: root.manaCurve

                        delegate: ColumnLayout {
                            required property int index
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            spacing: 3

                            Item {
                                Layout.fillWidth: true
                                Layout.fillHeight: true

                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: root.maxManaCount > 0
                                        ? parent.height * Number(modelData)
                                            / root.maxManaCount : 0
                                    color: Theme.primary
                                    opacity: index === 0 ? 0.35 : 0.7
                                    radius: 2
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                text: root.manaLabel(index)
                                color: Theme.textMuted
                                font.pixelSize: Theme.textPixelSize(10)
                                horizontalAlignment: Text.AlignHCenter
                            }
                        }
                    }
                }

                RecentPicksGallery {
                    id: recentPicksGallery
                    objectName: "recentPicksGallery"
                    Layout.fillWidth: true
                    pool: root.pool
                    narrow: root.narrow
                }
            }

            Item { Layout.fillHeight: true }
        }
    }
}
