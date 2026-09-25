[app]
# This checked-in spec is for unsigned development bundles only.
title = Draftomen-unsigned-macos
project_dir = .
input_file = draftomen/qt_gui.py
exec_directory = dist-native/macos-unsigned
project_file = pyproject.toml
icon = draftomen/assets/draftomen.icns

[python]
# Leave interpreter selection to the uv environment invoking pyside6-deploy.
python_path =
# Keep the Nuitka deployment tool explicit and pin its version in CI/local setup.
packages = Nuitka==4.1.3

[qt]
# Every QML file and the module manifest are listed deliberately. The deploy tool
# also preserves draftomen/assets/ so NavigationRail.qml and AboutDialog.qml can
# resolve the logo.
qml_files = draftomen/qml/AboutDialog.qml,draftomen/qml/PrivacyDialog.qml,draftomen/qml/TestDraftDialog.qml,draftomen/qml/AppBar.qml,draftomen/qml/BacktestView.qml,draftomen/qml/BuildView.qml,draftomen/qml/CardPreview.qml,draftomen/qml/DimensionalButton.qml,draftomen/qml/DimensionalComboBox.qml,draftomen/qml/DimensionalSurface.qml,draftomen/qml/DimensionalTabButton.qml,draftomen/qml/LiveDraftView.qml,draftomen/qml/Main.qml,draftomen/qml/NavigationRail.qml,draftomen/qml/PoolSummaryPanel.qml,draftomen/qml/RecentPickThumbnail.qml,draftomen/qml/RecentPicksGallery.qml,draftomen/qml/RecommendationRow.qml,draftomen/qml/SettingsView.qml,draftomen/qml/SettingsSwitch.qml,draftomen/qml/SettingsTextField.qml,draftomen/qml/StateBanner.qml,draftomen/qml/StatusStrip.qml,draftomen/qml/Theme.qml,draftomen/qml/qmldir
# QtTest stays bundled: the native smoke driver in qt_gui.py imports it, and
# excluding it strips Qt6Test.dll from the Windows build.
excluded_qml_plugins = QtCharts,QtQuick3D,QtSensors,QtWebEngine
modules = Core,Gui,Qml,Quick,QuickControls2
plugins = imageformats,platforms,platformthemes,styles

[nuitka]
mode = onefile
# The developer Test Draft imports socketio lazily inside draftomen/draftmancer.py,
# so the native build must carry the optional transport explicitly. The package is
# supplied by the locked draftmancer extra synced by the bundle workflow.
extra_args = --quiet --noinclude-qt-translations --include-data-files=draftomen/baseline_profiles/hob-quickdraft.json=draftomen/baseline_profiles/hob-quickdraft.json --company-name="Draft Omen" --product-name="Draft Omen" --file-version=0.4.0 --product-version=0.4.0 --macos-app-name="Draft Omen" --macos-app-version=0.4.0 --macos-signed-app-name=io.github.andreagrandi.draftomen --include-package=socketio
macos.permissions =

# Fonts: none are bundled. The application intentionally uses Qt's system fixed
# font so the bundle has no machine-specific font input.

