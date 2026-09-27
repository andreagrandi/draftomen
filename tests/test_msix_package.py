import configparser
import shutil
import tomllib
from pathlib import Path

import pytest

from scripts import msix_package
from scripts.msix_package import (
    MsixPackageError,
    msix_version,
    stage_package,
    validate_package,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "Draftomen-unsigned-windows.exe"
    path.write_bytes(data=b"MZ frozen app")
    return path


@pytest.fixture
def layout(tmp_path: Path, executable: Path) -> Path:
    output = tmp_path / "layout"
    stage_package(executable=executable, output=output, version="0.4.1.0")
    return output


def _edit_manifest(layout: Path, old: str, new: str) -> None:
    manifest_path = layout / "AppxManifest.xml"
    manifest = manifest_path.read_text(encoding="utf-8")
    assert old in manifest
    manifest_path.write_text(data=manifest.replace(old, new), encoding="utf-8")


def test_project_version_maps_to_four_part_version_with_zero_revision() -> None:
    assert msix_version(project_version="0.4.1") == "0.4.1.0"
    assert msix_version(project_version="12.0.65535") == "12.0.65535.0"


@pytest.mark.parametrize(
    argnames="project_version",
    argvalues=["0.4", "0.4.1.2", "0.4.1rc1", "0.4.1-dev", "01.4.1", "0.65536.0", ""],
)
def test_invalid_project_version_is_rejected(project_version: str) -> None:
    with pytest.raises(expected_exception=MsixPackageError):
        msix_version(project_version=project_version)


def test_version_higher_than_previous_release_is_accepted() -> None:
    assert (
        msix_version(project_version="0.4.2", previous_version="v0.4.1")
        == "0.4.2.0"
    )
    assert (
        msix_version(project_version="0.10.0", previous_version="0.9.9")
        == "0.10.0.0"
    )


@pytest.mark.parametrize(argnames="previous_version", argvalues=["v0.4.1", "0.5.0"])
def test_duplicate_or_regressing_version_fails(previous_version: str) -> None:
    with pytest.raises(expected_exception=MsixPackageError, match="does not increase"):
        msix_version(project_version="0.4.1", previous_version=previous_version)


def test_staged_package_holds_executable_manifest_and_logos(
    layout: Path,
    executable: Path,
) -> None:
    assert (layout / "DraftOmen.exe").read_bytes() == executable.read_bytes()
    assert sorted(
        path.relative_to(layout).as_posix() for path in layout.rglob("*")
    ) == [
        "AppxManifest.xml",
        "Assets",
        "Assets/Square150x150Logo.png",
        "Assets/Square44x44Logo.png",
        "Assets/StoreLogo.png",
        "Assets/Wide310x150Logo.png",
        "DraftOmen.exe",
    ]
    manifest = (layout / "AppxManifest.xml").read_text(encoding="utf-8")
    assert 'Version="0.4.1.0"' in manifest
    assert "${" not in manifest


def test_manifest_uses_partner_center_identity_verbatim(layout: Path) -> None:
    manifest = (layout / "AppxManifest.xml").read_text(encoding="utf-8")
    assert 'Name="27809AndreaGrandi.DraftOmen"' in manifest
    assert 'Publisher="CN=FF9C1E5C-B365-484C-9D4E-4AA6808AAC3D"' in manifest
    assert "<PublisherDisplayName>Andrea Grandi</PublisherDisplayName>" in manifest
    assert 'ProcessorArchitecture="x64"' in manifest
    assert 'MinVersion="10.0.17763.0"' in manifest


def test_manifest_display_metadata_matches_the_windows_bundle() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        description = tomllib.load(project_file)["project"]["description"]
    spec = configparser.ConfigParser()
    spec.read(filenames=PROJECT_ROOT / "pysidedeploy.windows.spec")
    extra_args = spec["nuitka"]["extra_args"]
    manifest = msix_package.MANIFEST_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert '--product-name="Draft Omen"' in extra_args
    assert "<DisplayName>Draft Omen</DisplayName>" in manifest
    assert 'DisplayName="Draft Omen"' in manifest
    assert f"<Description>{description}</Description>" in manifest
    assert f'Description="{description}"' in manifest


def test_project_version_comes_from_pyproject() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        version = tomllib.load(project_file)["project"]["version"]
    assert msix_package.read_project_version() == version


@pytest.mark.parametrize(
    argnames=("old", "new", "message"),
    argvalues=[
        (
            'Name="27809AndreaGrandi.DraftOmen"',
            'Name="27809AndreaGrandi.DraftOmenTest"',
            "Identity Name",
        ),
        (
            'Publisher="CN=FF9C1E5C-B365-484C-9D4E-4AA6808AAC3D"',
            'Publisher="CN=Draft Omen"',
            "Identity Publisher",
        ),
        (
            "<PublisherDisplayName>Andrea Grandi</PublisherDisplayName>",
            "<PublisherDisplayName>Someone Else</PublisherDisplayName>",
            "PublisherDisplayName",
        ),
        (
            'ProcessorArchitecture="x64"',
            'ProcessorArchitecture="arm64"',
            "Architecture",
        ),
        ('Version="0.4.1.0"', 'Version="0.4.1.1"', "X.Y.Z.0"),
        ('Version="0.4.1.0"', 'Version="0.4.1"', "X.Y.Z.0"),
        ('Name="Windows.Desktop"', 'Name="Windows.Universal"', "TargetDeviceFamily"),
        ('MinVersion="10.0.17763.0"', 'MinVersion="10.0.10240.0"', "MinVersion"),
        ('Executable="DraftOmen.exe"', 'Executable="bin\\DraftOmen.exe"', "Executable"),
        (
            'EntryPoint="Windows.FullTrustApplication"',
            'EntryPoint="DraftOmen.App"',
            "EntryPoint",
        ),
        (
            '<rescap:Capability Name="runFullTrust" />',
            '<rescap:Capability Name="runFullTrust" />'
            '<rescap:Capability Name="broadFileSystemAccess" />',
            "runFullTrust",
        ),
        (
            '<rescap:Capability Name="runFullTrust" />',
            '<Capability Name="internetClient" />',
            "runFullTrust",
        ),
        (
            'Square44x44Logo="Assets\\Square44x44Logo.png"',
            'Square44x44Logo="Assets\\Other.png"',
            "Manifest logos",
        ),
    ],
)
def test_invalid_manifest_fails_validation(
    layout: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    _edit_manifest(layout=layout, old=old, new=new)
    with pytest.raises(expected_exception=MsixPackageError, match=message):
        validate_package(layout=layout)


def test_missing_executable_fails_validation(layout: Path) -> None:
    (layout / "DraftOmen.exe").unlink()
    with pytest.raises(expected_exception=MsixPackageError, match="DraftOmen.exe"):
        validate_package(layout=layout)


def test_missing_logo_fails_validation(layout: Path) -> None:
    (layout / "Assets" / "StoreLogo.png").unlink()
    with pytest.raises(expected_exception=MsixPackageError, match="StoreLogo.png"):
        validate_package(layout=layout)


def test_wrong_logo_size_fails_validation(layout: Path) -> None:
    shutil.copy2(
        src=layout / "Assets" / "StoreLogo.png",
        dst=layout / "Assets" / "Square150x150Logo.png",
    )
    with pytest.raises(expected_exception=MsixPackageError, match="expected 150x150"):
        validate_package(layout=layout)


def test_staging_refuses_a_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(expected_exception=MsixPackageError, match="does not exist"):
        stage_package(
            executable=tmp_path / "missing.exe",
            output=tmp_path / "layout",
            version="0.4.1.0",
        )


def test_staging_refuses_an_existing_output_folder(
    layout: Path,
    executable: Path,
) -> None:
    with pytest.raises(expected_exception=MsixPackageError, match="already exists"):
        stage_package(executable=executable, output=layout, version="0.4.1.0")


def test_main_stages_the_package_and_prints_the_version(
    tmp_path: Path,
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        target=msix_package,
        name="read_project_version",
        value=lambda: "0.4.1",
    )
    output = tmp_path / "layout"

    exit_code = msix_package.main(
        argv=["--executable", str(executable), "--output", str(output)]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "0.4.1.0\n"
    validate_package(layout=output)


def test_main_fails_before_staging_when_the_version_does_not_increase(
    tmp_path: Path,
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        target=msix_package,
        name="read_project_version",
        value=lambda: "0.4.1",
    )
    output = tmp_path / "layout"

    exit_code = msix_package.main(
        argv=[
            "--executable",
            str(executable),
            "--output",
            str(output),
            "--previous-version",
            "v0.4.1",
        ]
    )

    assert exit_code == 1
    assert "does not increase" in capsys.readouterr().err
    assert not output.exists()
