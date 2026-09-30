pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
    id: root

    required property var pool
    required property bool narrow

    readonly property var recentPicks: {
        const logicalPicks = root.pool ? root.pool.recent_logical_picks : null
        if (logicalPicks && logicalPicks.length > 0) {
            const flattened = []
            for (let index = 0; index < logicalPicks.length; index++) {
                const cards = logicalPicks[index] && logicalPicks[index].cards
                    ? logicalPicks[index].cards : []
                for (let cardIndex = 0; cardIndex < cards.length; cardIndex++)
                    flattened.push(cards[cardIndex])
            }
            return flattened
        }
        return root.pool && root.pool.recent_picks
            ? root.pool.recent_picks : []
    }
    readonly property int targetColumns: root.narrow ? 4 : 6
    readonly property int gap: 6
    readonly property int hoverDelay: 501
    readonly property int dismissDelay: 100

    property var hoveredThumbnail: null
    property var previewedPick: null
    property bool thumbnailHovered: false
    property bool previewHovered: false

    implicitHeight: galleryColumn.implicitHeight
    Accessible.role: Accessible.Pane
    Accessible.name: "Recent picks"
    Accessible.description: "Cards picked most recently, newest first."

    function pickForThumbnail(thumbnail) {
        return thumbnail && thumbnail.recentPick && thumbnail.recentPick.card
            ? thumbnail.recentPick : null
    }

    function containsPick(pick) {
        if (!pick || !pick.card)
            return false
        for (let index = 0; index < root.recentPicks.length; index++) {
            const recentPick = root.recentPicks[index]
            if (recentPick && recentPick.card
                    && recentPick.card.grp_id === pick.card.grp_id)
                return true
        }
        return false
    }
    function handleThumbnailEntered(thumbnail) {
        const pick = root.pickForThumbnail(thumbnail)
        if (!pick)
            return
        dismissTimer.stop()
        if (root.hoveredThumbnail === thumbnail && root.thumbnailHovered)
            return

        if (root.hoveredThumbnail === thumbnail && previewCard.visible
                && root.previewedPick && root.previewedPick.card
                && pick.card
                && root.previewedPick.card.grp_id === pick.card.grp_id) {
            root.thumbnailHovered = true
            root.previewedPick = pick
            previewTimer.stop()
            return
        }

        root.hoveredThumbnail = thumbnail
        root.thumbnailHovered = true
        root.previewedPick = null
        previewTimer.restart()
    }

    function handleThumbnailExited(thumbnail) {
        if (root.hoveredThumbnail !== thumbnail)
            return
        root.thumbnailHovered = false
        previewTimer.stop()
        dismissTimer.restart()
    }

    function dismissPreviewIfOutside() {
        if (root.thumbnailHovered || root.previewHovered)
            return
        root.clearHoverState()
    }

    function showCurrentPreview() {
        const pick = root.pickForThumbnail(root.hoveredThumbnail)
        if (!root.thumbnailHovered || !pick
                || !root.containsPick(pick))
            return
        root.previewedPick = pick
        root.schedulePreviewGeometry()
    }

    function schedulePreviewGeometry() {
        if (!root.previewedPick || !previewCard.parent)
            return
        Qt.callLater(root.updatePreviewGeometry)
    }

    function updatePreviewGeometry() {
        const overlay = previewCard.parent
        if (!overlay || !root.hoveredThumbnail || !root.previewedPick
                || !previewCard.visible)
            return

        const galleryTopLeft = root.mapToItem(overlay, 0, 0)
        const galleryBottomRight = root.mapToItem(
            overlay, root.width, root.height
        )
        const margin = 8
        const gap = 8
        const availableAbove = Math.max(
            0, galleryTopLeft.y - gap - margin
        )
        const availableBelow = Math.max(
            0, overlay.height - galleryBottomRight.y - gap - margin
        )
        const maximumWidth = Math.min(
            260, Math.max(1, overlay.width - margin * 2)
        )
        previewCard.previewWidth = Math.min(
            maximumWidth,
            Math.max(1, Math.max(availableAbove, availableBelow) / 1.4)
        )

        const maximumX = Math.max(
            margin, overlay.width - previewCard.width - margin
        )
        const nextX = Math.max(
            margin, Math.min(galleryTopLeft.x, maximumX)
        )
        const belowY = galleryBottomRight.y + gap
        const aboveY = galleryTopLeft.y - previewCard.height - gap
        const maximumY = Math.max(
            margin, overlay.height - previewCard.height - margin
        )
        const belowFits = belowY >= margin && belowY <= maximumY
        const aboveFits = aboveY >= margin && aboveY <= maximumY
        let nextY
        if (availableBelow >= availableAbove && belowFits) {
            nextY = belowY
        } else if (aboveFits) {
            nextY = aboveY
        } else {
            nextY = availableBelow >= availableAbove ? belowY : aboveY
            nextY = Math.max(margin, Math.min(nextY, maximumY))
        }

        previewCard.x = nextX
        previewCard.y = nextY
    }

    function syncRecentPicks() {
        const hoveredPick = root.pickForThumbnail(root.hoveredThumbnail)
        if (hoveredPick && root.containsPick(hoveredPick)) {
            if (root.previewedPick)
                root.previewedPick = hoveredPick
            return
        }
        root.clearHoverState()
    }

    function clearHoverState() {
        previewTimer.stop()
        dismissTimer.stop()
        root.hoveredThumbnail = null
        root.thumbnailHovered = false
        root.previewHovered = false
        root.previewedPick = null
    }

    onRecentPicksChanged: Qt.callLater(root.syncRecentPicks)
    onWidthChanged: root.schedulePreviewGeometry()
    onHeightChanged: root.schedulePreviewGeometry()
    onVisibleChanged: {
        if (!root.visible)
            root.clearHoverState()
    }

    Column {
        id: galleryColumn
        width: root.width
        spacing: 8

        Label {
            id: heading
            width: parent.width
            text: "RECENT PICKS"
            color: Theme.textMuted
            font.pixelSize: Theme.textPixelSize(11)
            font.bold: true
            font.letterSpacing: 1
        }

        Grid {
            id: recentPicksGrid
            objectName: "recentPicksGrid"
            width: parent.width
            columns: Math.min(root.targetColumns, Math.max(1,
                Math.floor((width + root.gap) / (64 + root.gap))))
            columnSpacing: root.gap
            rowSpacing: root.gap
            height: childrenRect.height

            Repeater {
                model: root.recentPicks

                delegate: RecentPickThumbnail {
                    required property int index
                    required property var modelData

                    pickIndex: index
                    recentPick: modelData
                    width: Math.max(1, (recentPicksGrid.width
                        - (recentPicksGrid.columns - 1) * root.gap)
                        / recentPicksGrid.columns)
                    onHoverEntered: root.handleThumbnailEntered(this)
                    onHoverExited: root.handleThumbnailExited(this)
                }
            }
        }
    }
    Rectangle {
        id: previewCard
        objectName: "recentPickPreview"
        parent: root.Window.window ? root.Window.window.contentItem : root
        z: 100
        property bool modal: false
        property real previewWidth: Math.min(
            260, Math.max(200, parent ? parent.width - 16 : 200)
        )
        focus: false
        width: previewWidth
        height: width * 1.4
        visible: root.previewedPick !== null
        color: Theme.surface
        border.color: Theme.outline
        border.width: 1
        radius: Theme.radius
        clip: true
        Accessible.role: Accessible.Pane
        Accessible.name: root.previewedPick && root.previewedPick.card
            ? root.previewedPick.card.name + " preview" : "Card preview"

        Image {
            id: previewImage
            objectName: "recentPickPreviewImage"
            anchors.fill: parent
            anchors.margins: 12
            source: root.previewedPick && root.previewedPick.image
                && root.previewedPick.image.phase === "ready"
                && root.previewedPick.image.image_path
                ? root.previewedPick.image.image_path : ""
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            visible: status === Image.Ready
        }

        Label {
            id: previewFallbackLabel
            objectName: "recentPickPreviewFallbackLabel"
            anchors.fill: parent
            anchors.margins: 28
            text: {
                const image = root.previewedPick
                    ? root.previewedPick.image : null
                if (previewImage.status === Image.Error)
                    return "Image failed to display"
                if (image && image.phase === "loading")
                    return "Loading image"
                if (image && image.phase === "failed")
                    return "Image unavailable"
                if (image && image.phase === "ready"
                        && previewImage.source.toString().length > 0)
                    return "Loading image"
                return "No image available"
            }
            color: Theme.textMuted
            font.pixelSize: Theme.textPixelSize(15)
            font.bold: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.Wrap
            visible: !previewImage.visible
            Accessible.name: text
        }
        MouseArea {
            id: previewHoverArea
            anchors.fill: parent
            z: 1
            hoverEnabled: true
            acceptedButtons: Qt.NoButton

            onEntered: {
                root.previewHovered = true
                dismissTimer.stop()
            }
            onExited: {
                root.previewHovered = false
                if (root.thumbnailHovered)
                    dismissTimer.stop()
                else
                    dismissTimer.restart()
            }
        }
    }
    Connections {
        id: previewOverlayConnections
        target: previewCard.parent

        function onWidthChanged() {
            root.schedulePreviewGeometry()
        }

        function onHeightChanged() {
            root.schedulePreviewGeometry()
        }
    }

    Timer {
        id: previewTimer
        objectName: "recentPickHoverTimer"
        interval: root.hoverDelay
        repeat: false
        onTriggered: root.showCurrentPreview()
    }

    Timer {
        id: dismissTimer
        objectName: "recentPickDismissTimer"
        interval: root.dismissDelay
        repeat: false
        onTriggered: root.dismissPreviewIfOutside()
    }
}
