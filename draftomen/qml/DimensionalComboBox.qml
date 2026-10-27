pragma ComponentBehavior: Bound

import QtQuick 2.15
import QtQml 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ComboBox {
    id: control

    property bool accented: true
    property color accentColor: Theme.primary
    property color accentContentColor: Theme.primaryContent
    readonly property color contentColor: {
        if (!control.enabled)
            return Theme.controlDisabledContent
        return control.accented ? control.accentContentColor : Theme.neutralContent
    }

    implicitWidth: 150
    implicitHeight: Theme.targetHeight

    function labelFor(model, index) {
        if (!control.textRole)
            return control.textAt(index)

        let roleValue = model[control.textRole]
        if (control.textRole === "label" && !roleValue)
            roleValue = model.pair
        if (control.textRole === "screen_name" && !roleValue)
            return model.account_id ? String(model.account_id) : ""

        if (roleValue === undefined || roleValue === null)
            return ""

        const label = String(roleValue)
        if ((control.textRole === "pair" || control.textRole === "label") && model.automatic)
            return label + " · automatic"
        return label
    }

    background: DimensionalSurface {
        stateEnabled: control.enabled
        stateHovered: control.hovered
        statePressed: control.pressed
        stateFocused: control.activeFocus
        stateSelected: control.currentIndex >= 0
        stateExpanded: control.popup.visible
        accented: control.accented
        accentColor: control.accentColor
    }

    contentItem: Text {
        transform: Translate {
            y: control.pressed ? Theme.controlPressOffset : 0
        }
        leftPadding: 12
        rightPadding: control.indicator.width + 10
        text: control.displayText
        color: control.contentColor
        font: control.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: Canvas {
        id: chevron
        objectName: "dimensionalComboIndicator"
        x: control.width - width - 10
        y: (control.height - height) / 2
            + (control.pressed ? Theme.controlPressOffset : 0)
        width: 14
        height: 10
        opacity: control.enabled ? 1 : 0.45
        rotation: control.popup.visible ? 180 : 0
        onPaint: {
            const context = getContext("2d")
            context.clearRect(0, 0, width, height)
            context.strokeStyle = control.contentColor
            context.lineWidth = 1.5
            context.lineCap = "square"
            context.beginPath()
            context.moveTo(2, 3)
            context.lineTo(7, 8)
            context.lineTo(12, 3)
            context.stroke()
        }
        onRotationChanged: requestPaint()
        onOpacityChanged: requestPaint()
    }
    Connections {
        target: control
        function onContentColorChanged() {
            chevron.requestPaint()
        }
    }

    delegate: ItemDelegate {
        id: comboDelegate
        objectName: "dimensionalComboDelegate"
        required property var model
        required property int index

        width: comboPopup.availableWidth
        height: Theme.targetHeight
        text: control.labelFor(comboDelegate.model, comboDelegate.index)
        highlighted: control.highlightedIndex === comboDelegate.index
        Accessible.name: text

        background: DimensionalSurface {
            stateEnabled: comboDelegate.enabled
            stateHovered: comboDelegate.hovered || comboDelegate.highlighted
            statePressed: comboDelegate.down
            stateFocused: comboDelegate.activeFocus
            stateSelected: control.currentIndex === comboDelegate.index
            stateExpanded: comboDelegate.highlighted
            accented: control.currentIndex === comboDelegate.index
            accentColor: control.accentColor
        }

        contentItem: RowLayout {
            anchors.fill: parent
            anchors.topMargin: comboDelegate.down ? Theme.controlPressOffset : 0
            anchors.leftMargin: 10
            anchors.rightMargin: 10
            spacing: 8

            Text {
                objectName: "dimensionalComboSelectedIndicator"
                visible: control.currentIndex === comboDelegate.index
                text: "✓"
                color: comboDelegate.enabled
                    ? control.accentContentColor : Theme.controlDisabledContent
                font.bold: true
                Layout.preferredWidth: 14
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                Accessible.ignored: true
            }

            Text {
                Layout.fillWidth: true
                text: comboDelegate.text
                color: {
                    if (!comboDelegate.enabled)
                        return Theme.controlDisabledContent
                    if (control.currentIndex === comboDelegate.index)
                        return control.accentContentColor
                    return Theme.neutralContent
                }
                font: comboDelegate.font
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }
    }

    popup: Popup {
        id: comboPopup
        objectName: "dimensionalComboPopup"
        y: control.height + 4
        width: control.width
        padding: 4
        implicitHeight: Math.min(320, comboList.contentHeight + padding * 2)
        modal: false

        background: DimensionalSurface {
            stateEnabled: control.enabled
            stateExpanded: true
            accented: false
        }

        contentItem: ListView {
            id: comboList
            objectName: "dimensionalComboList"
            clip: true
            implicitHeight: contentHeight
            model: comboPopup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds
            ScrollIndicator.vertical: ScrollIndicator { }
        }
    }
}
