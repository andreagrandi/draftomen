pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    objectName: "liveDraftView"

    required property var sessionState
    required property var recommendationModel
    required property bool narrow
    required property var displayPreferences

    ButtonGroup {
        id: wideRecommendationFilterGroup
        exclusive: true
    }
    ButtonGroup {
        id: narrowRecommendationFilterGroup
        exclusive: true
    }

    // Keep the documented 1440px layout wide while making room for the
    // larger details pane; the recommendation list remains scrollable.
    // As the window narrows, the list shrinks to its minimum first, then the
    // details pane shrinks from its full width to its minimum. Below both
    // minimums the view falls back to the stacked layout.
    readonly property int wideCardDetailsWidth: 550
    readonly property int wideCardDetailsMinimumWidth: 350
    readonly property int wideRecommendationsMinimumListWidth: 662
    readonly property int wideRecommendationsMinimumWidth:
        root.wideRecommendationsMinimumListWidth
            + root.wideCardDetailsMinimumWidth + Theme.gutter
    readonly property int wideCardDetailsCurrentWidth: Math.max(
        root.wideCardDetailsMinimumWidth,
        Math.min(
            root.wideCardDetailsWidth,
            root.width - root.wideRecommendationsMinimumListWidth
                - Theme.gutter
        )
    )
    // The pool keeps enough height for the whole mana curve, so the preview
    // gives up height first, down to the smallest size that fits its image
    // frame and stats.
    readonly property int wideCardPreviewPreferredHeight: 430
    readonly property int wideCardPreviewMinimumHeight: 290
    readonly property int widePoolDetailsMinimumHeight: 270
    readonly property int wideRecommendationsMinimumHeight:
        root.wideCardPreviewMinimumHeight + Theme.gutter
            + root.widePoolDetailsMinimumHeight
    readonly property real wideRecommendationsAvailableHeight:
        root.height - draftHeader.implicitHeight
            - (stateBanner.visible ? stateBanner.implicitHeight : 0)
            - Theme.gutter * 2
    readonly property bool wideRecommendations: !root.narrow
        && root.width >= root.wideRecommendationsMinimumWidth
        && root.wideRecommendationsAvailableHeight
            >= root.wideRecommendationsMinimumHeight

    // Preserve the normal recommendation/detail balance while reserving
    // enough height for the details tab at the supported compact minimum.
    readonly property int narrowRecommendationPreferredHeight: 220
    readonly property int narrowDetailsPreferredHeight: 340
    readonly property int narrowDetailsMinimumHeight: 276
    readonly property int narrowLayoutSpacing: 8
    readonly property real narrowRecommendationAvailableHeight:
        root.height - draftHeader.implicitHeight
            - (stateBanner.visible ? stateBanner.implicitHeight : 0)
            - Theme.gutter * 2
            - detailTabs.implicitHeight - root.narrowLayoutSpacing * 2
            - root.narrowDetailsMinimumHeight
    readonly property real narrowRecommendationMinimumHeight: Math.max(
        0, Math.min(
            root.narrowRecommendationPreferredHeight,
            root.narrowRecommendationAvailableHeight
        )
    )
    readonly property bool hasRecommendations: sessionState.recommendations
        && sessionState.recommendations.cards
        && sessionState.recommendations.cards.length > 0
    readonly property string recommendationFilterMode:
        root.recommendationModel
            ? root.recommendationModel.filterMode : ""
    readonly property bool hasSetupGuidance: Boolean(
        sessionState.status && sessionState.status.setup_guidance
    )
    readonly property string confidenceSummary: {
        const recommendations = sessionState.recommendations
        return recommendations && recommendations.confidence_summary
            ? String(recommendations.confidence_summary) : ""
    }

    readonly property var draftProgress: sessionState.draft_progress || null
    readonly property string draftFormatName: root.draftProgress
        && root.draftProgress.known && root.draftProgress.format_name
        ? String(root.draftProgress.format_name) : ""
    readonly property string draftProgressText: {
        const progress = root.draftProgress
        if (!progress || !progress.known
                || progress.pack_number === null
                || progress.pack_number === undefined
                || progress.pick_number === null
                || progress.pick_number === undefined)
            return ""
        let text = "Pack " + (progress.pack_number + 1)
            + " of " + progress.pack_count
            + " · Pick " + (progress.pick_number + 1)
            + " of " + progress.picks_per_pack
        if (progress.cards_per_pick > 1)
            text += " · take " + progress.cards_per_pick + " cards"
        return text
    }

    readonly property string draftHeading: {
        const draft = sessionState.draft
        if (!draft) {
            if (root.hasSetupGuidance)
                return "Arena setup needed"
            return sessionState.status.message
        }
        if (draft.completed)
            return "Draft complete"
        if (draft.pack_number === null || draft.pick_number === null)
            return draft.event_name
        if (root.draftProgressText.length > 0)
            return root.draftProgressText
        return "Pack " + (draft.pack_number + 1)
            + " · Pick " + (draft.pick_number + 1)
    }
    readonly property string emptyHeading: {
        if (sessionState.draft && sessionState.draft.completed)
            return "Draft complete"
        if (sessionState.status.phase === "starting")
            return "Loading live draft data"
        if (root.hasSetupGuidance)
            return "Arena setup needed"
        if (root.draftFormatName.length > 0)
            return root.draftFormatName + " detected"
        return "Ready for your next draft"
    }
    readonly property var selectedRecommendation: {
        const recommendations = sessionState.recommendations
        if (!recommendations || !recommendations.cards)
            return null
        const cards = recommendations.cards
        for (let index = 0; index < cards.length; index++) {
            const recommendation = cards[index]
            if (recommendation.card.grp_id === recommendations.selected_grp_id)
                return recommendation
        }
        return cards.length > 0 ? cards[0] : null
    }
    readonly property var testDraft: sessionState.test_draft || null
    readonly property var moxgate: sessionState.moxgate || null
    readonly property string moxgatePhase: root.moxgate && root.moxgate.phase
        ? String(root.moxgate.phase) : "stopped"
    readonly property string moxgateError: root.moxgate && root.moxgate.error
        ? String(root.moxgate.error) : ""
    readonly property string moxgateText: {
        if (root.moxgatePhase === "starting")
            return "Moxgate · loading card data"
        if (root.moxgatePhase === "waiting")
            return "Moxgate · waiting for the extension on " + String(root.moxgate.endpoint)
        if (root.moxgatePhase === "receiving")
            return "Moxgate · receiving a draft"
        return ""
    }
    readonly property bool testDraftActive: root.testDraft !== null && root.testDraft.active === true
    readonly property bool testDraftManual: root.testDraftActive && root.testDraft.mode === "manual"
    readonly property bool testDraftPending: root.testDraft !== null && root.testDraft.pending === true
    readonly property string testDraftError: root.testDraft && root.testDraft.error
        ? String(root.testDraft.error) : ""
    readonly property int testDraftOfferGeneration: root.testDraft
        ? Number(root.testDraft.offer_generation) : 0
    readonly property bool testDraftCanPick: root.testDraftManual
        && root.testDraft.phase === "drafting"
        && !root.testDraftPending
        && root.testDraftOfferGeneration > 0
        && root.selectedRecommendation !== null
    readonly property int testDraftCardsPerPick: root.testDraft
        && root.testDraft.cards_per_pick ? Number(root.testDraft.cards_per_pick) : 1
    readonly property var testDraftOfferedGrpIds: root.testDraft
        && root.testDraft.offered_grp_ids ? root.testDraft.offered_grp_ids : []
    readonly property string testDraftFormatLabel: {
        const testDraft = root.testDraft
        if (!testDraft || !testDraft.draft_format)
            return ""
        const formats = testDraft.supported_formats || []
        for (let index = 0; index < formats.length; index++) {
            if (formats[index].key === testDraft.draft_format)
                return String(formats[index].label)
        }
        return String(testDraft.draft_format)
    }
    readonly property bool testDraftStagingPicks: root.testDraftCardsPerPick === 2
    // The first card of a Pick-Two pick waits here until the second is chosen.
    property int stagedGrpId: -1
    property string stagedName: ""
    readonly property bool testDraftHasStagedPick: root.stagedGrpId >= 0
    readonly property int testDraftSelectedCopies: {
        const selected = root.selectedRecommendation
        if (!selected)
            return 0
        let copies = 0
        for (let index = 0; index < root.testDraftOfferedGrpIds.length; index++) {
            if (root.testDraftOfferedGrpIds[index] === selected.card.grp_id)
                copies++
        }
        return copies
    }
    readonly property bool testDraftSecondPickAllowed: {
        const selected = root.selectedRecommendation
        if (!root.testDraftHasStagedPick || !selected)
            return true
        return selected.card.grp_id !== root.stagedGrpId
            || root.testDraftSelectedCopies >= 2
    }
    readonly property string testDraftPickText: {
        if (!root.testDraftStagingPicks)
            return "Pick"
        return root.testDraftHasStagedPick ? "Pick 2 of 2" : "Pick 1 of 2"
    }

    function clearStagedPick() {
        root.stagedGrpId = -1
        root.stagedName = ""
    }

    function confirmTestDraftPick() {
        const selected = root.selectedRecommendation
        if (!selected)
            return
        if (!root.testDraftStagingPicks) {
            sessionProvider.pickTestDraft([selected.card.grp_id], root.testDraftOfferGeneration)
            return
        }
        if (!root.testDraftHasStagedPick) {
            root.stagedGrpId = selected.card.grp_id
            root.stagedName = String(selected.card.name)
            return
        }
        const staged = root.stagedGrpId
        root.clearStagedPick()
        sessionProvider.pickTestDraft(
            [staged, selected.card.grp_id], root.testDraftOfferGeneration
        )
    }

    onTestDraftOfferGenerationChanged: root.clearStagedPick()
    onTestDraftManualChanged: root.clearStagedPick()

    property bool recommendationFocusPublishedWhileVisible: false

    onVisibleChanged: {
        if (!root.visible) {
            root.recommendationFocusPublishedWhileVisible = false
            return
        }
        Qt.callLater(function() {
            if (!root.visible || root.recommendationFocusPublishedWhileVisible
                    || !root.selectedRecommendation) {
                return
            }
            root.recommendationFocusPublishedWhileVisible = true
            sessionProvider.chooseRecommendation(
                root.selectedRecommendation.card.grp_id
            )
        })
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.gutter

        RowLayout {
            id: draftHeader
            Layout.fillWidth: true
            spacing: 16

            ColumnLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 2

                Label {
                    objectName: "liveDraftHeading"
                    text: root.draftHeading
                    color: Theme.text
                    font.pixelSize: Theme.textPixelSize(22)
                    font.bold: true
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }

                Label {
                    objectName: "liveDraftStatus"
                    text: root.hasRecommendations
                        ? (root.draftFormatName.length > 0
                            ? root.draftFormatName + " · " : "")
                            + root.sessionState.recommendations.cards.length
                            + " cards available"
                        : root.sessionState.status.message
                    color: Theme.textMuted
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }
            }

            ColumnLayout {
                visible: root.hasRecommendations
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignVCenter
                spacing: 2

                Label {
                    objectName: "recommendationConfidenceSummary"
                    visible: root.confidenceSummary.length > 0
                    text: root.confidenceSummary
                    color: Theme.text
                    font.pixelSize: Theme.textPixelSize(12)
                    font.bold: true
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                }
            }

            ColumnLayout {
                visible: root.testDraftActive
                Layout.alignment: Qt.AlignVCenter
                spacing: 2

                Label {
                    objectName: "testDraftIndicator"
                    // The capability may be absent from the published state, so the
                    // binding must not read through a null record.
                    text: root.testDraft
                        ? "Mocked Draft · " + root.testDraftFormatLabel + " · "
                            + String(root.testDraft.mode) + " · "
                            + String(root.testDraft.set_code).toUpperCase()
                        : ""
                    color: Theme.primary
                    font.pixelSize: Theme.textPixelSize(12)
                    font.bold: true
                }

                Label {
                    objectName: "testDraftError"
                    visible: root.testDraftError.length > 0
                    text: root.testDraftError
                    color: Theme.error
                    font.pixelSize: Theme.textPixelSize(12)
                    Layout.maximumWidth: 320
                    wrapMode: Text.WordWrap
                }
            }

            ColumnLayout {
                objectName: "moxgateColumn"
                visible: root.moxgatePhase !== "stopped"
                Layout.alignment: Qt.AlignVCenter
                spacing: 2

                Label {
                    objectName: "moxgateIndicator"
                    visible: root.moxgateText.length > 0
                    text: root.moxgateText
                    color: Theme.primary
                    font.pixelSize: Theme.textPixelSize(12)
                    font.bold: true
                }

                Label {
                    objectName: "moxgateError"
                    visible: root.moxgateError.length > 0
                    text: root.moxgateError
                    color: Theme.error
                    font.pixelSize: Theme.textPixelSize(12)
                    Layout.maximumWidth: 320
                    wrapMode: Text.WordWrap
                }
            }

            DimensionalButton {
                objectName: "testDraftPickButton"
                visible: root.testDraftManual
                enabled: root.testDraftCanPick && root.testDraftSecondPickAllowed
                accented: true
                text: root.testDraftPickText
                Layout.alignment: Qt.AlignVCenter
                Accessible.name: root.testDraftStagingPicks
                    ? "Confirm Mocked Draft " + root.testDraftPickText.toLowerCase()
                    : "Confirm Mocked Draft pick"
                Accessible.description: {
                    if (!root.testDraftStagingPicks)
                        return "Submit the selected card to the simulated draft."
                    return root.testDraftHasStagedPick
                        ? "Submit the staged card and the selected card to the simulated draft."
                        : "Stage the selected card as the first card of this pick."
                }
                onClicked: root.confirmTestDraftPick()
            }

            Label {
                objectName: "testDraftStagedPick"
                visible: root.testDraftManual && root.testDraftHasStagedPick
                text: "Staged: " + root.stagedName
                color: Theme.primary
                font.pixelSize: Theme.textPixelSize(12)
                elide: Text.ElideRight
                Layout.alignment: Qt.AlignVCenter
                Layout.maximumWidth: 180
                Accessible.name: "Staged Mocked Draft card " + root.stagedName
            }

            DimensionalButton {
                objectName: "testDraftClearStagedPick"
                visible: root.testDraftManual && root.testDraftHasStagedPick
                enabled: !root.testDraftPending
                accented: false
                text: "Clear"
                Layout.alignment: Qt.AlignVCenter
                Accessible.name: "Clear staged Mocked Draft card"
                Accessible.description: "Discard the staged first card and choose it again."
                onClicked: root.clearStagedPick()
            }

            DimensionalComboBox {
                id: rankingSelector
                objectName: "rankingSelector"
                Layout.preferredWidth: root.narrow ? 138 : 166
                model: [
                    { key: "score", label: "DO Score" },
                    { key: "win_rate", label: "17L WR" },
                    { key: "alsa", label: "ALSA" },
                    { key: "mana_value", label: "Mana value" }
                ]
                textRole: "label"
                valueRole: "key"
                currentIndex: {
                    const recommendations = root.sessionState.recommendations
                    if (!recommendations)
                        return 0
                    for (let index = 0; index < model.length; index++)
                        if (model[index].key === recommendations.ranking_mode)
                            return index
                    return 0
                }
                Accessible.name: "Recommendation ranking"
                Accessible.description: "Choose DO Score, 17L WR, ALSA, or mana value."
                onActivated: sessionProvider.changeRanking(currentValue)
            }
        }

        StateBanner {
            id: stateBanner
            objectName: "liveStateBanner"
            Layout.fillWidth: true
            sessionState: root.sessionState
        }

        Rectangle {
            visible: !root.hasRecommendations
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: Theme.surfaceLow
            border.color: Theme.outline
            border.width: 1
            radius: Theme.radius

            ColumnLayout {
                anchors.centerIn: parent
                width: Math.min(parent.width - 48, 520)
                spacing: 14

                Label {
                    objectName: "preDraftHeading"
                    Layout.fillWidth: true
                    text: root.emptyHeading
                    color: Theme.text
                    font.pixelSize: Theme.textPixelSize(22)
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                }

                Label {
                    objectName: "preDraftGuidance"
                    Layout.fillWidth: true
                    text: root.hasSetupGuidance
                        ? root.sessionState.status.message
                        : "Draft Omen follows Arena automatically and never writes to the game."
                    color: root.hasSetupGuidance ? Theme.text : Theme.textMuted
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                }

                Repeater {
                    model: [
                        ["Card metadata", root.sessionState.card_data.message],
                        ["Arena log", root.sessionState.status.message],
                        ["Arena account", root.sessionState.active_account
                            ? root.sessionState.active_account.screen_name
                                || root.sessionState.active_account.account_id
                            : "Not detected"],
                        ["Draft", root.sessionState.draft
                            ? (root.draftFormatName.length > 0
                                ? root.draftFormatName + " · " : "")
                                + root.sessionState.draft.event_name
                            : "No draft detected"],
                        ["Ratings", root.sessionState.ratings.message]
                    ]

                    delegate: Rectangle {
                        required property var modelData

                        Layout.fillWidth: true
                        Layout.preferredHeight: 46
                        color: Theme.surface
                        radius: Theme.radius

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12

                            Label {
                                Layout.preferredWidth: 126
                                text: modelData[0]
                                color: Theme.text
                                font.bold: true
                            }
                            Label {
                                Layout.fillWidth: true
                                text: modelData[1]
                                color: Theme.textMuted
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }
        }

        RowLayout {
            visible: root.wideRecommendations && root.hasRecommendations
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.gutter

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: root.wideRecommendationsMinimumListWidth
                color: "transparent"

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 6

                    RowLayout {
                        objectName: "wideRecommendationFilter"
                        Layout.fillWidth: true
                        Layout.leftMargin: 14
                        Layout.rightMargin: 12
                        spacing: 8

                        Label {
                            text: "Card filter"
                            color: Theme.textMuted
                            font.pixelSize: Theme.textPixelSize(11)
                            font.bold: true
                        }

                        DimensionalButton {
                            objectName: "wideRecommendationOnColorFilter"
                            text: "On Color"
                            checkable: true
                            checked: root.recommendationFilterMode === "on_color"
                            accented: checked
                            ButtonGroup.group: wideRecommendationFilterGroup
                            Accessible.name: "On Color"
                            Accessible.description:
                                "Show only current-pick cards matching the current draft colors."
                            onClicked: {
                                if (root.recommendationModel)
                                    root.recommendationModel.setFilterMode(
                                        "on_color"
                                    )
                            }
                        }

                        DimensionalButton {
                            objectName: "wideRecommendationAllFilter"
                            text: "All"
                            checkable: true
                            checked: root.recommendationFilterMode === "all"
                            accented: checked
                            ButtonGroup.group: wideRecommendationFilterGroup
                            Accessible.name: "All"
                            Accessible.description:
                                "Show every card in the current pick."
                            onClicked: {
                                if (root.recommendationModel)
                                    root.recommendationModel.setFilterMode("all")
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 14
                        Layout.rightMargin: 12
                        spacing: 8

                        Label {
                            objectName: "recommendationHeaderRank"
                            Layout.preferredWidth: 30
                            text: "#"
                            color: Theme.textMuted
                            font.pixelSize: Theme.textPixelSize(11)
                        }
                        Label {
                            objectName: "recommendationHeaderCard"
                            Layout.fillWidth: true
                            text: "CARD"
                            color: Theme.textMuted
                            font.pixelSize: Theme.textPixelSize(11)
                        }
                        Label { Layout.preferredWidth: 70; text: "COLORS"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11); horizontalAlignment: Text.AlignHCenter }
                        Label { Layout.preferredWidth: 58; text: "DO"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11); horizontalAlignment: Text.AlignRight }
                        Label { Layout.preferredWidth: 68; text: "17L WR"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11); horizontalAlignment: Text.AlignRight }
                        Label { Layout.preferredWidth: 44; text: "GRADE"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11); horizontalAlignment: Text.AlignHCenter }
                        Label { Layout.preferredWidth: 82; text: "FIT"; color: Theme.textMuted; font.pixelSize: Theme.textPixelSize(11); horizontalAlignment: Text.AlignRight }
                    }

                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        delegate: RecommendationRow {
                            required property var modelData
                            objectName: "wideRecommendationRow" + modelData.rank

                            width: ListView.view.width
                            recommendation: modelData
                            selected: root.sessionState.recommendations.selected_grp_id
                                === modelData.card.grp_id
                            wide: root.wideRecommendations
                            secondaryStats: root.displayPreferences.secondaryStats
                            onChosen: grpId => sessionProvider.chooseRecommendation(grpId)
                        }
                        clip: true
                        model: root.wideRecommendations
                            ? root.recommendationModel : null
                        Accessible.name: "Ranked recommendations"

                    }
                }
            }

            ColumnLayout {
                id: wideCardDetailsColumn
                objectName: "wideCardDetailsColumn"
                Layout.preferredWidth: root.wideCardDetailsCurrentWidth
                Layout.maximumWidth: root.wideCardDetailsWidth
                Layout.fillHeight: true
                Layout.minimumWidth: root.wideCardDetailsMinimumWidth
                spacing: Theme.gutter

                CardPreview {
                    objectName: "wideLiveCardPreview"
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.max(
                        root.wideCardPreviewMinimumHeight,
                        Math.min(
                            root.wideCardPreviewPreferredHeight,
                            wideCardDetailsColumn.height - Theme.gutter
                                - widePoolDetails.manaCurveRequiredHeight
                        )
                    )
                    Layout.minimumHeight: root.wideCardPreviewMinimumHeight
                    recommendation: root.selectedRecommendation
                    detailedIntel: true
                    detailedImageMaximumWidth: 250
                    detailedImageWidthRatio: 0.58
                    loading: root.sessionState.card_data.phase === "loading"
                    imageState: root.sessionState.card_image
                    constrainImageFrameToHeight: true
                }

                PoolSummaryPanel {
                    id: widePoolDetails
                    objectName: "wideLivePoolDetails"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: root.widePoolDetailsMinimumHeight
                    pool: root.sessionState.pool
                    narrow: root.narrow
                }
            }
        }

    ColumnLayout {
        visible: !root.wideRecommendations && root.hasRecommendations
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: root.narrowLayoutSpacing

        ListView {
            objectName: "narrowRecommendationList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: root.narrowRecommendationMinimumHeight
            spacing: root.displayPreferences.compactDensity ? 3 : 6
            clip: true
            model: root.wideRecommendations ? null : root.recommendationModel
            Accessible.name: "Ranked recommendations"

            delegate: RecommendationRow {
                required property var modelData
                objectName: "narrowRecommendationRow" + modelData.rank

                width: ListView.view.width
                recommendation: modelData
                selected: root.sessionState.recommendations.selected_grp_id
                    === modelData.card.grp_id
                wide: false
                secondaryStats: root.displayPreferences.secondaryStats
                onChosen: grpId => sessionProvider.chooseRecommendation(grpId)
            }
        }

        // Share the existing detail-tab allocation so the filter stays by
        // the list without adding another vertical row.
        RowLayout {
            id: narrowRecommendationControls
            objectName: "narrowRecommendationControls"
            Layout.fillWidth: true
            spacing: root.narrowLayoutSpacing

            RowLayout {
                id: narrowRecommendationFilter
                objectName: "narrowRecommendationFilter"
                spacing: 8

                Label {
                    text: "Card filter"
                    color: Theme.textMuted
                    font.pixelSize: Theme.textPixelSize(11)
                    font.bold: true
                }

                DimensionalButton {
                    objectName: "narrowRecommendationOnColorFilter"
                    text: "On Color"
                    checkable: true
                    checked: root.recommendationFilterMode === "on_color"
                    accented: checked
                    ButtonGroup.group: narrowRecommendationFilterGroup
                    Accessible.name: "On Color"
                    Accessible.description:
                        "Show only current-pick cards matching the current draft colors."
                    onClicked: {
                        if (root.recommendationModel)
                            root.recommendationModel.setFilterMode("on_color")
                    }
                }

                DimensionalButton {
                    objectName: "narrowRecommendationAllFilter"
                    text: "All"
                    checkable: true
                    checked: root.recommendationFilterMode === "all"
                    accented: checked
                    ButtonGroup.group: narrowRecommendationFilterGroup
                    Accessible.name: "All"
                    Accessible.description:
                        "Show every card in the current pick."
                    onClicked: {
                        if (root.recommendationModel)
                            root.recommendationModel.setFilterMode("all")
                    }
                }
            }

            TabBar {
                id: detailTabs
                objectName: "liveDetailTabs"
                Layout.fillWidth: true
                currentIndex: 0
                Accessible.name: "Live draft details"

                DimensionalTabButton {
                    objectName: "liveCardDetailsTab"
                    text: "Card details"
                    Accessible.name: "Card details"
                }
                DimensionalTabButton {
                    objectName: "livePoolTab"
                    text: "Pool"
                    Accessible.name: "Pool details"
                }
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: root.narrowDetailsPreferredHeight
            Layout.minimumHeight: root.narrowDetailsMinimumHeight
            currentIndex: detailTabs.currentIndex

            CardPreview {
                objectName: "narrowLiveCardPreview"
                recommendation: root.selectedRecommendation
                detailedIntel: true
                loading: root.sessionState.card_data.phase === "loading"
                imageState: root.sessionState.card_image
                constrainImageFrameToHeight: true
            }

            PoolSummaryPanel {
                objectName: "narrowLivePoolDetails"
                pool: root.sessionState.pool
                narrow: root.narrow
            }
        }
    }
    }
}

