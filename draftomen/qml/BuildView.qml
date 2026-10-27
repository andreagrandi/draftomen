pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    objectName: "buildView"

    required property var sessionState
    required property bool narrow
    required property var displayPreferences
    readonly property var rawBuild: root.sessionState ? root.sessionState.build : null
    readonly property bool hasBuild: root.rawBuild !== null && root.rawBuild !== undefined
    readonly property var build: root.hasBuild ? root.rawBuild : ({
        selected_pair: "",
        pair_options: [],
        spells: [],
        lands: [],
        bench: [],
        deck_size: 0,
        average_mana_value: null,
        pair_override: null,
        spell_count: null,
        warnings: [],
        land_count: null,
        creature_count: null,
        instant_count: null
    })
    readonly property int wideMinimumWidth: 220 + 360 + 240 + Theme.gutter * 2
    readonly property int wideBuildCardMinimumHeight: 300
    readonly property real wideBuildContextMinimumHeight:
        wideReasoning.implicitHeight + Theme.panelPadding * 2
    readonly property real wideBenchHeight: (
        root.benchExpanded
            ? Math.min(
                wideBenchRows.implicitHeight,
                root.wideBenchViewportMaximumHeight
            )
            : 0
    ) + Theme.targetHeight + wideBenchContent.spacing + 4 * 2
    // Keep the expanded bench compact so the main row retains useful height.
    readonly property real wideBuildMainRowMinimumHeight:
        root.wideBuildCardMinimumHeight
            + (root.displayPreferences.detailedBuildContext
                ? wideBuildCardColumn.spacing + root.wideBuildContextMinimumHeight : 0)
    readonly property real wideBenchViewportAvailableHeight:
        wideBuildContent.height
            - root.wideBuildMainRowMinimumHeight
            - wideBuildContent.spacing
            - Theme.targetHeight
            - wideBenchContent.spacing
            - 4 * 2
    readonly property real wideBuildMinimumContentHeight:
        Math.max(
            wideBuildSummaryColumn.implicitHeight,
            root.wideBuildMainRowMinimumHeight
        )
            + wideBuildContent.spacing
            + Theme.targetHeight
            + wideBenchContent.spacing
            + 4 * 2
    readonly property real wideBenchViewportMaximumHeight: Math.max(
        root.wideBenchViewportAvailableHeight >= Theme.targetHeight + Theme.gutter
            ? Theme.targetHeight + Theme.gutter : 0,
        Math.min(
            root.height * 0.2,
            root.wideBenchViewportAvailableHeight
        )
    )
    readonly property bool compactPresentation: root.narrow
        || root.width < root.wideMinimumWidth
        || wideBuildContent.height < root.wideBuildMinimumContentHeight
    readonly property var automaticPair: {
        for (let index = 0; index < root.build.pair_options.length; index++) {
            if (root.build.pair_options[index].automatic)
                return root.build.pair_options[index]
        }
        return null
    }
    readonly property string selectedPairLabel: root.build.selected_pair_label || root.build.selected_pair
    readonly property string pairDescription: {
        if (!root.build.pair_override)
            return root.selectedPairLabel + " was selected automatically from the drafted pool."
        const automaticPairName = root.automaticPair ? (root.automaticPair.label || root.automaticPair.pair) : "another pair"
        return root.selectedPairLabel + " was explicitly requested. Automatic evaluation preferred " + automaticPairName + "."
    }
    readonly property string buildIdentity: {
        if (!root.hasBuild)
            return ""
        return root.build.selected_pair + ":"
            + root.build.spells.map(entry => entry.card.grp_id).join(",") + ":"
            + root.build.bench.map(entry => entry.card.grp_id).join(",")
    }
    property var focusedCard: null
    property string publishedBuildFocusKey: ""
    property bool spellsExpanded: true
    property bool landsExpanded: false
    property bool benchExpanded: false
    property bool contextExpanded: false
    property bool cardDetailsExpanded: false

    property bool buildFocusPublishedWhileVisible: false

    onVisibleChanged: {
        if (!root.visible) {
            root.buildFocusPublishedWhileVisible = false
            return
        }
        Qt.callLater(function() {
            if (!root.visible || root.buildFocusPublishedWhileVisible
                    || !root.focusedCard) {
                return
            }
            root.buildFocusPublishedWhileVisible = true
            root.publishBuildFocus(root.focusedCard, true)
        })
    }

    onBuildIdentityChanged: {
        root.focusedCard = root.hasBuild && root.build.spells.length > 0 ? root.build.spells[0] : null
        root.cardDetailsExpanded = false
        if (!root.focusedCard) {
            root.publishedBuildFocusKey = ""
            return
        }
        if (!root.visible)
            return
        Qt.callLater(function() {
            root.publishBuildFocus(root.focusedCard, false)
        })
    }

    function publishBuildFocus(card, force) {
        if (!card)
            return
        const focusKey = root.buildIdentity + ":" + card.card.grp_id
        if (!force && focusKey === root.publishedBuildFocusKey)
            return
        root.publishedBuildFocusKey = focusKey
        sessionProvider.focusBuildCard(card.card.grp_id)
    }

    function focusCardDetailsToggle() {
        if (!cardDetailsToggle.visible)
            return
        narrowBuildScroll.ScrollBar.vertical.position = 0
        cardDetailsToggle.forceActiveFocus()
    }

    function focusCard(card) {
        root.focusedCard = card
        root.publishBuildFocus(card, true)
        if (root.compactPresentation && root.displayPreferences.cardPreview) {
            root.cardDetailsExpanded = true
            narrowBuildScroll.ScrollBar.vertical.position = 0
            Qt.callLater(function() {
                Qt.callLater(root.focusCardDetailsToggle)
            })
        }
    }

    function spellManaValue(spell) {
        if (!spell || !spell.card || spell.card.mana_value === null || spell.card.mana_value === undefined)
            return null
        return spell.card.mana_value
    }

    function averageManaValueText() {
        const average = root.build.average_mana_value
        if (average === null || average === undefined)
            return "Average mana value: —"
        return "Average mana value: " + Number(average).toFixed(2)
    }
    function countText(value) {
        return value === null || value === undefined ? "—" : value
    }
    function countLabel(value, singular) {
        const text = root.countText(value)
        return text + " " + singular + (text === 1 ? "" : "s")
    }

    function manaBucket(spell) {
        const manaValue = root.spellManaValue(spell)
        if (manaValue === null)
            return 6
        if (manaValue <= 1)
            return 0
        return manaValue >= 6 ? 6 : manaValue
    }

    function manaBucketLabel(bucket) {
        if (bucket === 0)
            return "MV 0/1"
        return bucket === 6 ? "MV 6+" : "MV " + bucket
    }

    function manaCount(bucket) {
        let count = 0
        for (let index = 0; index < root.build.spells.length; index++) {
            if (root.manaBucket(root.build.spells[index]) === bucket)
                count += root.build.spells[index].quantity
        }
        return count
    }

    function benchQuantity() {
        let count = 0
        for (let index = 0; index < root.build.bench.length; index++)
            count += root.build.bench[index].quantity
        return count
    }

    function spellsInBucket(bucket) {
        const spellEntries = []
        for (let index = 0; index < root.build.spells.length; index++) {
            const spell = root.build.spells[index]
            if (root.manaBucket(spell) === bucket)
                spellEntries.push({ cardEntry: spell, entryIndex: index })
        }
        return spellEntries
    }

    function landSummary(land) {
        if (land.card)
            return "drafted nonbasic"
        return "basic source " + land.source_colors.join("/")
    }

    component ManaPips: Row {
        id: manaPips
        required property var colors
        spacing: 3
        Repeater {
            model: manaPips.colors
            delegate: Rectangle {
                required property string modelData
                width: 16
                height: 16
                radius: 8
                color: Theme.colorForMana(modelData)
                border.color: Theme.outline
                border.width: 1
                Label {
                    anchors.centerIn: parent
                    text: modelData
                    color: Theme.background
                    font.pixelSize: Theme.textPixelSize(9)
                    font.bold: true
                }
            }
        }
        Label {
            visible: manaPips.colors.length === 0
            text: "C"
            color: Theme.textMuted
            font.pixelSize: Theme.textPixelSize(10)
            font.bold: true
        }
    }

    component ManaCurve: Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: Math.max(
            154,
            manaCurveContent.implicitHeight + Theme.panelPadding * 2
        )
        color: Theme.surfaceLow
        border.color: Theme.outline
        border.width: 1
        radius: Theme.radius
        Accessible.role: Accessible.Pane
        Accessible.name: "Mana curve"
        Accessible.description: "Horizontally labelled mana curve for the suggested deck."

        ColumnLayout {
            id: manaCurveContent
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: Theme.panelPadding
            anchors.rightMargin: Theme.panelPadding
            anchors.topMargin: Theme.panelPadding
            spacing: 8
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Label {
                        objectName: "manaCurveTitle"
                        Layout.fillWidth: true
                        text: "MANA CURVE"
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(10)
                        font.bold: true
                        font.letterSpacing: 1
                    }
                    Label {
                        objectName: "manaCurveAverage"
                        Layout.fillWidth: true
                        text: root.averageManaValueText()
                        color: Theme.textMuted
                        font.pixelSize: Theme.textPixelSize(11)
                        Accessible.name: text
                    }
                }
                Label {
                    Layout.fillWidth: true
                    text: "spells by mana value"
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(11)
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 8
                Repeater {
                    model: [0, 2, 3, 4, 5, 6]
                    delegate: ColumnLayout {
                        required property int modelData
                        readonly property int cardCount: root.manaCount(modelData)
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 4
                        Label {
                            Layout.alignment: Qt.AlignHCenter
                            text: cardCount
                            color: Theme.text
                            font.family: fixedFontFamily
                            font.bold: true
                            Accessible.name: cardCount + " cards at " + root.manaBucketLabel(modelData)
                        }
                        Item { Layout.fillHeight: true }
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(4, Math.min(56, cardCount * 7))
                            color: Theme.primary
                            radius: 2
                        }
                        Label {
                            Layout.alignment: Qt.AlignHCenter
                            text: root.manaBucketLabel(modelData).replace("MV ", "")
                            color: Theme.textMuted
                            font.pixelSize: Theme.textPixelSize(10)
                            font.bold: true
                        }
                    }
                }
            }
        }
    }

    component BuildCardRow: DimensionalButton {
        id: cardRow
        required property var cardEntry
        required property int entryIndex
        readonly property bool selected: root.focusedCard !== null
            && root.focusedCard.card.grp_id === cardEntry.card.grp_id
        readonly property bool accessibilitySelectable: Accessible.selectable
        readonly property bool accessibilitySelected: Accessible.selected
        readonly property string colorsText: cardEntry.card.colors.length > 0
            ? cardEntry.card.colors.join(" · ") : "Colorless"
        readonly property string scoreText: cardEntry.score !== null && cardEntry.score !== undefined
            ? cardEntry.score : "—"
        Layout.fillWidth: true
        Layout.minimumHeight: 40
        Layout.preferredHeight: root.displayPreferences.compactDensity ? 40 : 46
        text: ""
        dimensionalSelected: cardRow.selected
        accented: cardRow.selected
        Accessible.name: "Focus card " + cardEntry.card.name
        Accessible.description: cardEntry.quantity + " copies, " + colorsText
            + ", DO score " + scoreText + ", grade " + (cardEntry.letter_grade || "—")
            + ", " + (cardEntry.color_fit || "—")
        Accessible.selectable: true
        Accessible.selected: cardRow.selected
        onClicked: root.focusCard(cardEntry)
        contentItem: RowLayout {
            anchors.fill: parent
            anchors.topMargin: cardRow.down ? Theme.controlPressOffset : 0
            anchors.leftMargin: 10
            anchors.rightMargin: 10
            spacing: 8
            Label {
                Layout.preferredWidth: 24
                text: "×" + cardEntry.quantity
                color: cardRow.selected ? cardRow.contentColor : Theme.textMuted
                font.family: fixedFontFamily
                horizontalAlignment: Text.AlignRight
            }
            ManaPips { colors: cardEntry.card.colors }
            Label {
                Layout.fillWidth: true
                text: cardEntry.card.name
                color: cardRow.contentColor
                font.bold: cardRow.selected
                elide: Text.ElideRight
            }
            Label {
                Layout.preferredWidth: 30
                text: cardEntry.letter_grade || "—"
                color: cardRow.selected ? cardRow.contentColor : Theme.warning
                font.bold: true
                horizontalAlignment: Text.AlignRight
            }
            Label {
                Layout.preferredWidth: 42
                text: "DO " + scoreText
                color: cardRow.selected ? cardRow.contentColor : Theme.primary
                font.family: fixedFontFamily
                horizontalAlignment: Text.AlignRight
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.gutter

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gutter
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Label { text: "Suggested deck"; color: Theme.text; font.pixelSize: Theme.textPixelSize(22); font.bold: true }
                Label {
                    Layout.fillWidth: true
                    text: "Recreate this build in Arena · Draft Omen remains read only"
                    color: Theme.textMuted
                    wrapMode: Text.WordWrap
                }
            }
            Label {
                visible: root.hasBuild
                text: root.build.pair_override ? "OVERRIDE ACTIVE" : "AUTOMATIC PAIR"
                color: root.build.pair_override ? Theme.warning : Theme.primary
                font.pixelSize: Theme.textPixelSize(10)
                font.bold: true
                font.letterSpacing: 1
            }
            DimensionalComboBox {
                id: pairSelector
                objectName: "buildPairSelector"
                visible: root.hasBuild
                Layout.preferredWidth: root.compactPresentation ? 128 : 200
                model: root.build.pair_options
                textRole: "label"
                valueRole: "pair"
                currentIndex: {
                    for (let index = 0; index < root.build.pair_options.length; index++) {
                        if (root.build.pair_options[index].pair === root.build.selected_pair)
                            return index
                    }
                    return 0
                }
                Accessible.name: "Deck color pair"
                Accessible.description: "Choose a pair, then rebuild the suggested deck."
            }
            DimensionalButton {
                objectName: "buildRebuildButton"
                visible: root.hasBuild
                text: "Rebuild"
                Accessible.name: "Rebuild suggested deck"
                Accessible.description: "Requests a new build with the selected color pair override."
                onClicked: {
                    sessionProvider.requestBuild(pairSelector.currentValue || "")
                }
            }
        }

        StateBanner { Layout.fillWidth: true; sessionState: root.sessionState }

        Rectangle {
            visible: !root.hasBuild
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: Theme.surfaceLow
            border.color: Theme.outline
            border.width: 1
            radius: Theme.radius
            ColumnLayout {
                anchors.centerIn: parent
                width: Math.min(parent.width - 40, 440)
                spacing: 12
                Label { text: "No deck build available"; color: Theme.text; font.pixelSize: Theme.textPixelSize(20); font.bold: true }
                Label { Layout.fillWidth: true; text: "Complete or recover a draft to request a suggested deck."; color: Theme.textMuted; wrapMode: Text.WordWrap }
                DimensionalButton {
                    objectName: "buildRequestButton"
                    text: "Request build"
                    Accessible.name: "Request suggested deck build"
                    onClicked: sessionProvider.requestBuild("")
                }
            }
        }

        Item {
            visible: root.hasBuild
            Layout.fillWidth: true
            Layout.fillHeight: true

            ColumnLayout {
                id: wideBuildContent
                visible: !root.compactPresentation
                anchors.fill: parent
                spacing: Theme.gutter

                RowLayout {
                    id: wideBuildMainRow
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: root.wideBuildMainRowMinimumHeight
                    spacing: Theme.gutter

                ColumnLayout {
                    id: wideBuildSummaryColumn
                    Layout.fillWidth: true
                    Layout.minimumWidth: 220
                    Layout.preferredWidth: 300
                    Layout.maximumWidth: 360
                    Layout.horizontalStretchFactor: 3
                    Layout.fillHeight: true
                    spacing: Theme.gutter

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: composition.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: composition
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 10
                            Label { text: "DECK COMPOSITION"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true; font.letterSpacing: 1 }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 20
                                rowSpacing: 8
                                Label { text: root.build.deck_size; color: Theme.text; font.pixelSize: Theme.textPixelSize(22); font.bold: true }
                                Label { text: root.countText(root.build.spell_count); color: Theme.primary; font.pixelSize: Theme.textPixelSize(22); font.bold: true }
                                Label { text: "TOTAL CARDS"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Label { text: "SPELLS"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Label { objectName: "wideBuildCreatureCount"; text: root.countText(root.build.creature_count); color: Theme.primary; font.pixelSize: Theme.textPixelSize(18); font.bold: true }
                                Label { objectName: "wideBuildInstantCount"; text: root.countText(root.build.instant_count); color: Theme.primary; font.pixelSize: Theme.textPixelSize(18); font.bold: true }
                                Label { text: "CREATURES"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Label { text: "INSTANTS"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Label { text: root.countText(root.build.land_count); color: Theme.warning; font.pixelSize: Theme.textPixelSize(18); font.bold: true }
                                Label { text: root.build.deck_size === 40 ? "DECK SIZE READY" : "DECK SIZE CHECK"; color: root.build.deck_size === 40 ? Theme.primary : Theme.warning; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Label { text: "LANDS"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Label { text: "SUGGESTED DECK"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                            }
                        }
                    }

                    Rectangle {
                        objectName: "wideBuildManaBase"
                        Layout.fillWidth: true
                        Layout.preferredHeight: manaBase.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: manaBase
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 7
                            Label { text: "MANA BASE"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true; font.letterSpacing: 1 }
                            Repeater {
                                model: root.build.lands
                                delegate: RowLayout {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    ManaPips { colors: modelData.source_colors }
                                    Label { Layout.fillWidth: true; text: modelData.name; color: Theme.text; elide: Text.ElideRight }
                                    Label { text: modelData.quantity; color: Theme.text; font.family: fixedFontFamily }
                                }
                            }
                        }
                    }
                    ManaCurve { objectName: "wideBuildManaCurve" }

                    Rectangle {
                        objectName: "wideBuildWarnings"
                        visible: root.build.warnings.length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: wideWarnings.implicitHeight + Theme.panelPadding * 2
                        color: Theme.warningDark
                        border.color: Theme.warning
                        border.width: 1
                        radius: Theme.radius
                        Label {
                            id: wideWarnings
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            text: root.build.warnings.join("\n")
                            color: Theme.warning
                            wrapMode: Text.WordWrap
                            Accessible.name: "Build warnings: " + text
                        }
                    }
                    Item { Layout.fillHeight: true }

                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 360
                    Layout.preferredWidth: 600
                    Layout.horizontalStretchFactor: 6
                    Layout.fillHeight: true
                    color: Theme.surfaceLow
                    border.color: Theme.outline
                    border.width: 1
                    radius: Theme.radius
                    clip: true
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 0
                        RowLayout {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 48
                            Layout.leftMargin: Theme.panelPadding
                            Layout.rightMargin: Theme.panelPadding
                            Label { text: "MAIN DECK SPELLS"; color: Theme.text; font.pixelSize: Theme.textPixelSize(12); font.bold: true; font.letterSpacing: 1 }
                            Item { Layout.fillWidth: true }
                            Label { text: root.build.spell_count !== null ? root.build.spell_count + " cards" : ""; color: Theme.textMuted; font.family: fixedFontFamily }
                        }
                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.outline }
                        ScrollView {
                            id: wideSpellScroll
                            objectName: "buildSpellGroups"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            contentWidth: availableWidth
                            ColumnLayout {
                                width: wideSpellScroll.availableWidth
                                spacing: 12
                                Repeater {
                                    model: [0, 2, 3, 4, 5, 6]
                                    delegate: ColumnLayout {
                                        required property int modelData
                                        readonly property int bucket: modelData
                                        Layout.fillWidth: true
                                        Layout.leftMargin: 8
                                        Layout.rightMargin: 8
                                        spacing: 3
                                        Rectangle {
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 28
                                            color: Theme.surface
                                            radius: Theme.radius
                                            Label {
                                                anchors.verticalCenter: parent.verticalCenter
                                                anchors.left: parent.left
                                                anchors.leftMargin: 10
                                                text: root.manaBucketLabel(modelData)
                                                color: Theme.textMuted
                                                font.pixelSize: Theme.textPixelSize(11)
                                                font.bold: true
                                                Accessible.name: text
                                            }
                                            Label {
                                                anchors.verticalCenter: parent.verticalCenter
                                                anchors.right: parent.right
                                                anchors.rightMargin: 10
                                                text: root.manaCount(modelData)
                                                color: Theme.textMuted
                                                font.family: fixedFontFamily
                                            }
                                        }
                                        Repeater {
                                            model: root.spellsInBucket(bucket)
                                            delegate: BuildCardRow {
                                                required property var modelData
                                                cardEntry: modelData.cardEntry
                                                entryIndex: modelData.entryIndex
                                                objectName: root.compactPresentation ? "" : "buildSpellButton" + entryIndex
                                            }
                                        }
                                        Label {
                                            visible: root.manaCount(modelData) === 0
                                            text: "No spells at this mana value"
                                            color: Theme.textMuted
                                            font.pixelSize: Theme.textPixelSize(11)
                                            leftPadding: 10
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                ColumnLayout {
                    id: wideBuildCardColumn
                    Layout.fillWidth: true
                    Layout.minimumWidth: 240
                    Layout.preferredWidth: 300
                    Layout.maximumWidth: 360
                    Layout.horizontalStretchFactor: 3
                    Layout.fillHeight: true
                    spacing: Theme.gutter
                    CardPreview {
                        id: wideBuildCardPreview
                        objectName: "wideBuildCardPreview"
                        constrainImageFrameToHeight: true
                        visible: root.displayPreferences.cardPreview
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: root.wideBuildCardMinimumHeight
                        recommendation: root.focusedCard
                        imageState: root.sessionState.card_image
                    }
                    Rectangle {
                        objectName: "wideBuildContext"
                        visible: root.displayPreferences.detailedBuildContext
                        Layout.fillWidth: true
                        Layout.preferredHeight: root.wideBuildContextMinimumHeight
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: wideReasoning
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.leftMargin: Theme.panelPadding
                            anchors.rightMargin: Theme.panelPadding
                            anchors.topMargin: Theme.panelPadding
                            spacing: 4
                            DimensionalButton {
                                objectName: "wideBuildContextToggle"
                                Layout.fillWidth: true
                                text: (root.contextExpanded ? "Hide" : "Show") + " why this pair"
                                expanded: root.contextExpanded
                                accented: false
                                Accessible.name: text
                                Accessible.description: root.contextExpanded
                                    ? "The pair rationale is currently expanded. Activating this button collapses it."
                                    : "The pair rationale is currently collapsed. Activating this button expands it."
                                onClicked: root.contextExpanded = !root.contextExpanded
                            }
                            ColumnLayout {
                                objectName: "wideBuildContextDetails"
                                visible: root.contextExpanded
                                Layout.fillWidth: true
                                spacing: 4
                                Label { text: "WHY THIS PAIR"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true; font.letterSpacing: 1 }
                                Label {
                                    Layout.fillWidth: true
                                    text: root.pairDescription
                                    color: Theme.text
                                    wrapMode: Text.WordWrap
                                }
                                Repeater {
                                    model: root.build.pair_options
                                    delegate: Label {
                                        required property var modelData
                                        objectName: "widePairOption" + modelData.pair
                                        Layout.fillWidth: true
                                        text: (modelData.label || modelData.pair) + " · score " + modelData.score.toFixed(1)
                                            + " · " + (modelData.playable_count !== null
                                                ? modelData.playable_count + " playables"
                                                : "playables unavailable")
                                            + (modelData.automatic ? " · automatic" : "")
                                        color: modelData.selected ? Theme.primary : Theme.textMuted
                                        wrapMode: Text.WordWrap
                                        Accessible.name: text
                                    }
                                }
                            }
                        }
                    }
                }
            }

                Rectangle {
                    objectName: "wideBuildBench"
                    Layout.fillWidth: true
                    Layout.minimumHeight: root.wideBenchHeight
                    Layout.preferredHeight: root.wideBenchHeight
                    Layout.maximumHeight: Math.max(
                        root.wideBenchHeight,
                        root.height * 0.45
                    )
                    color: Theme.surfaceLow
                    border.color: Theme.outline
                    border.width: 1
                    radius: Theme.radius
                    clip: true
                    ColumnLayout {
                        id: wideBenchContent
                        anchors.fill: parent
                        anchors.margins: 4
                        spacing: 4
                        DimensionalButton {
                            objectName: root.compactPresentation ? "" : "buildBenchToggle"
                            Layout.fillWidth: true
                            text: (root.benchExpanded ? "Hide" : "Show") + " bench · " + root.benchQuantity()
                            expanded: root.benchExpanded
                            accented: false
                            Accessible.name: "Toggle bench"
                            onClicked: root.benchExpanded = !root.benchExpanded
                        }
                        ScrollView {
                            id: wideBenchScroll
                            objectName: "wideBuildBenchScroll"
                            visible: root.benchExpanded
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.min(
                                wideBenchRows.implicitHeight,
                                root.wideBenchViewportMaximumHeight
                            )
                            Layout.maximumHeight: root.wideBenchViewportMaximumHeight
                            clip: true
                            contentWidth: availableWidth
                            ScrollBar.vertical.policy: ScrollBar.AsNeeded
                            ColumnLayout {
                                id: wideBenchRows
                                width: wideBenchScroll.availableWidth
                                spacing: 5
                                Repeater {
                                    model: root.build.bench
                                    delegate: BuildCardRow {
                                        required property var modelData
                                        required property int index
                                        cardEntry: modelData
                                        entryIndex: index
                                        objectName: root.compactPresentation ? "" : "buildBenchButton" + index
                                        onActiveFocusChanged: {
                                            if (!activeFocus)
                                                return
                                            const top = mapToItem(wideBenchRows, 0, 0).y
                                            const bottom = top + height
                                            const flickable = wideBenchScroll.contentItem
                                            if (top < flickable.contentY)
                                                flickable.contentY = top
                                            else if (bottom > flickable.contentY + wideBenchScroll.height)
                                                flickable.contentY = bottom - wideBenchScroll.height
                                        }
                                    }
                                }
                                Label {
                                    visible: root.build.bench.length === 0
                                    text: "No bench cards"
                                    color: Theme.textMuted
                                }
                            }
                        }
                    }
                }
            }

            ScrollView {
                id: narrowBuildScroll
                objectName: "narrowBuildScroll"
                visible: root.compactPresentation
                anchors.fill: parent
                clip: true
                contentWidth: availableWidth
                ColumnLayout {
                    width: narrowBuildScroll.availableWidth
                    spacing: Theme.gutter

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: narrowSummary.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: narrowSummary
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 6
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: root.build.deck_size; color: Theme.primary; font.pixelSize: Theme.textPixelSize(22); font.bold: true }
                                Label { text: "/ 40 CARDS"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10); font.bold: true }
                                Item { Layout.fillWidth: true }
                                Label {
                                    text: root.countText(root.build.spell_count) + " spells · "
                                        + root.countText(root.build.land_count) + " lands"
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.textPixelSize(11)
                                }
                            }
                            Label {
                                objectName: "narrowBuildTypeSummary"
                                Layout.fillWidth: true
                                text: root.countLabel(root.build.creature_count, "creature") + " · "
                                    + root.countLabel(root.build.instant_count, "instant")
                                color: Theme.textMuted
                                font.pixelSize: Theme.textPixelSize(11)
                            }
                            Label {
                                text: root.selectedPairLabel + " · " + (root.build.pair_override ? "override" : "automatic")
                                color: root.build.pair_override ? Theme.warning : Theme.primary
                                font.bold: true
                                Accessible.name: "Selected pair " + text
                            }
                            Label {
                                text: root.build.deck_size === 40 ? "DECK SIZE READY" : "DECK SIZE CHECK"
                                color: root.build.deck_size === 40 ? Theme.primary : Theme.warning
                                font.pixelSize: Theme.textPixelSize(10)
                                font.bold: true
                                font.letterSpacing: 1
                            }
                        }
                    }

                    Rectangle {
                        visible: root.build.warnings.length > 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: narrowWarnings.implicitHeight + Theme.panelPadding * 2
                        color: Theme.warningDark
                        border.color: Theme.warning
                        border.width: 1
                        radius: Theme.radius
                        Label {
                            id: narrowWarnings
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            text: root.build.warnings.join("\n")
                            color: Theme.warning
                            wrapMode: Text.WordWrap
                            Accessible.name: "Build warnings: " + text
                        }
                    }

                    ManaCurve { objectName: "narrowBuildManaCurve" }

                    DimensionalButton {
                        id: cardDetailsToggle
                        objectName: "buildCardDetailsToggle"
                        visible: root.displayPreferences.cardPreview && root.focusedCard
                        Layout.fillWidth: true
                        text: (root.cardDetailsExpanded ? "Hide" : "Show") + " selected card details"
                        expanded: root.cardDetailsExpanded
                        accented: false
                        Accessible.name: "Toggle selected card details"
                        onClicked: {
                            root.cardDetailsExpanded = !root.cardDetailsExpanded
                            if (!root.cardDetailsExpanded)
                                Qt.callLater(root.focusCardDetailsToggle)
                        }
                    }
                    CardPreview {
                        objectName: "narrowBuildCardPreview"
                        visible: root.displayPreferences.cardPreview && root.cardDetailsExpanded
                        Layout.fillWidth: true
                        Layout.preferredHeight: 250
                        recommendation: root.focusedCard
                        imageState: root.sessionState.card_image
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: spellsContent.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: spellsContent
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 5
                            DimensionalButton {
                                objectName: "buildSpellsToggle"
                                Layout.fillWidth: true
                                text: (root.spellsExpanded ? "Hide" : "Show") + " spells · " + (root.build.spell_count !== null ? root.build.spell_count : root.build.spells.length)
                                expanded: root.spellsExpanded
                                accented: false
                                Accessible.name: "Toggle deck spells"
                                onClicked: root.spellsExpanded = !root.spellsExpanded
                            }
                            Repeater {
                                visible: root.spellsExpanded
                                model: [0, 2, 3, 4, 5, 6]
                                delegate: ColumnLayout {
                                    required property int modelData
                                    readonly property int bucket: modelData
                                    Layout.fillWidth: true
                                    spacing: 3
                                    Label {
                                        text: root.manaBucketLabel(modelData) + " · " + root.manaCount(modelData)
                                        color: Theme.textMuted
                                        font.pixelSize: Theme.textPixelSize(10)
                                        font.bold: true
                                        font.letterSpacing: 1
                                        Accessible.name: text
                                    }
                                    Repeater {
                                        model: root.spellsInBucket(bucket)
                                        delegate: BuildCardRow {
                                            required property var modelData
                                            cardEntry: modelData.cardEntry
                                            entryIndex: modelData.entryIndex
                                            objectName: root.compactPresentation ? "buildSpellButton" + entryIndex : ""
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: landsContent.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: landsContent
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 5
                            DimensionalButton {
                                objectName: "buildLandsToggle"
                                Layout.fillWidth: true
                                text: (root.landsExpanded ? "Hide" : "Show") + " lands · " + root.build.land_count
                                expanded: root.landsExpanded
                                accented: false
                                Accessible.name: "Toggle lands"
                                onClicked: root.landsExpanded = !root.landsExpanded
                            }
                            Repeater {
                                visible: root.landsExpanded
                                model: root.build.lands
                                delegate: RowLayout {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    ManaPips { colors: modelData.source_colors }
                                    Label { Layout.fillWidth: true; text: "×" + modelData.quantity + " " + modelData.name; color: Theme.text; wrapMode: Text.WordWrap }
                                    Label { text: root.landSummary(modelData); color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(10) }
                                }
                            }
                        }
                    }

                    Rectangle {
                        objectName: "narrowBuildBench"
                        Layout.fillWidth: true
                        Layout.preferredHeight: benchContent.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: benchContent
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 5
                            DimensionalButton {
                                objectName: root.compactPresentation ? "buildBenchToggle" : ""
                                Layout.fillWidth: true
                                text: (root.benchExpanded ? "Hide" : "Show") + " bench · " + root.benchQuantity()
                                expanded: root.benchExpanded
                                accented: false
                                Accessible.name: "Toggle bench"
                                onClicked: root.benchExpanded = !root.benchExpanded
                            }
                            Repeater {
                                visible: root.benchExpanded
                                model: root.build.bench
                                delegate: BuildCardRow {
                                    visible: root.benchExpanded
                                    required property var modelData
                                    required property int index
                                    cardEntry: modelData
                                    entryIndex: index
                                    objectName: root.compactPresentation ? "buildBenchButton" + index : ""
                                }
                            }
                        }
                    }

                    Rectangle {
                        objectName: "narrowBuildContext"
                        visible: root.displayPreferences.detailedBuildContext
                        Layout.fillWidth: true
                        Layout.preferredHeight: contextContent.implicitHeight + Theme.panelPadding * 2
                        color: Theme.surfaceLow
                        border.color: Theme.outline
                        border.width: 1
                        radius: Theme.radius
                        ColumnLayout {
                            id: contextContent
                            anchors.fill: parent
                            anchors.margins: Theme.panelPadding
                            spacing: 6
                            DimensionalButton {
                                objectName: "narrowBuildContextToggle"
                                Layout.fillWidth: true
                                text: (root.contextExpanded ? "Hide" : "Show") + " why this pair"
                                expanded: root.contextExpanded
                                accented: false
                                Accessible.name: text
                                Accessible.description: root.contextExpanded
                                    ? "The pair rationale is currently expanded. Activating this button collapses it."
                                    : "The pair rationale is currently collapsed. Activating this button expands it."
                                onClicked: root.contextExpanded = !root.contextExpanded
                            }
                            ColumnLayout {
                                objectName: "narrowBuildContextDetails"
                                visible: root.contextExpanded
                                Layout.fillWidth: true
                                spacing: 6
                                Label {
                                    Layout.fillWidth: true
                                    text: root.pairDescription
                                    color: Theme.text
                                    wrapMode: Text.WordWrap
                                }
                                Repeater {
                                    model: root.build.pair_options
                                    delegate: Label {
                                        required property var modelData
                                        objectName: "narrowPairOption" + modelData.pair
                                        Layout.fillWidth: true
                                        text: (modelData.label || modelData.pair) + " · score " + modelData.score.toFixed(1)
                                            + " · " + (modelData.playable_count !== null
                                                ? modelData.playable_count + " playables"
                                                : "playables unavailable")
                                            + (modelData.automatic ? " · automatic" : "")
                                        color: modelData.selected ? Theme.primary : Theme.textMuted
                                        wrapMode: Text.WordWrap
                                        Accessible.name: text
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
