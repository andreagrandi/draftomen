"""Stage and validate the Microsoft Store MSIX package folder.
MakeAppx packs the folder this module writes; every check runs before that.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from string import Template
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
PACKAGING_DIR = PROJECT_ROOT / "packaging" / "windows"
MANIFEST_TEMPLATE_PATH = PACKAGING_DIR / "AppxManifest.xml"
ASSETS_DIR = PACKAGING_DIR / "Assets"

# Partner Center assigned these values; see issue #326.
IDENTITY_NAME = "27809AndreaGrandi.DraftOmen"
PUBLISHER = "CN=FF9C1E5C-B365-484C-9D4E-4AA6808AAC3D"
PUBLISHER_DISPLAY_NAME = "Andrea Grandi"
DISPLAY_NAME = "Draft Omen"
ARCHITECTURE = "x64"
DEVICE_FAMILY = "Windows.Desktop"
MIN_VERSION = "10.0.17763.0"
EXECUTABLE_NAME = "DraftOmen.exe"
# Nuitka names the standalone executable after draftomen/qt_gui.py.
NUITKA_EXECUTABLE_NAME = "qt_gui.exe"
LOGO_DIRECTORY_NAME = "Assets"
FULL_TRUST_ENTRY_POINT = "Windows.FullTrustApplication"

FOUNDATION_NS = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
UAP_NS = "http://schemas.microsoft.com/appx/manifest/uap/windows10"
RESCAP_NS = (
    "http://schemas.microsoft.com/appx/manifest/foundation/windows10/"
    "restrictedcapabilities"
)
REQUIRED_CAPABILITIES = {(RESCAP_NS, "runFullTrust")}

# Pixel sizes of every logo the manifest must reference.
REQUIRED_ASSET_SIZES = {
    "Assets/StoreLogo.png": (50, 50),
    "Assets/Square44x44Logo.png": (44, 44),
    "Assets/Square150x150Logo.png": (150, 150),
    "Assets/Wide310x150Logo.png": (310, 150),
}

MAX_VERSION_PART = 65535
_PROJECT_VERSION_PATTERN = re.compile(
    pattern=r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class MsixPackageError(ValueError):
    """Describe a package input or staged folder that cannot be packed.
    Keep failures readable for both the tests and the CI log.
    """


def parse_project_version(version: str) -> tuple[int, int, int]:
    """Parse an X.Y.Z project version or v-prefixed release tag.
    Reject prerelease suffixes and parts too large for an MSIX version.
    """
    match = _PROJECT_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise MsixPackageError(
            f"Version {version!r} is not a plain X.Y.Z release version."
        )
    parts = tuple(int(part) for part in match.groups())
    if any(part > MAX_VERSION_PART for part in parts):
        raise MsixPackageError(
            f"Version {version!r} has a part above {MAX_VERSION_PART}."
        )
    return parts[0], parts[1], parts[2]


def msix_version(
    project_version: str,
    previous_version: str | None = None,
) -> str:
    """Map an X.Y.Z project version to the X.Y.Z.0 MSIX version.
    With a previous release version, refuse a duplicate or lower version.
    """
    parts = parse_project_version(version=project_version)
    if previous_version is not None:
        previous_parts = parse_project_version(version=previous_version)
        if parts <= previous_parts:
            raise MsixPackageError(
                f"Version {project_version} does not increase on the previous "
                f"release {previous_version}."
            )
    # The Store reserves the fourth part, so it is always 0.
    return ".".join(str(part) for part in (*parts, 0))


def read_project_version(pyproject_path: Path = PYPROJECT_PATH) -> str:
    """Read the project version from pyproject.toml.
    The MSIX version is always derived from this single source.
    """
    with pyproject_path.open(mode="rb") as project_file:
        return tomllib.load(project_file)["project"]["version"]


def render_manifest(version: str) -> str:
    """Fill the checked-in manifest template with an MSIX version.
    Every other manifest value stays exactly as written in the template.
    """
    template = Template(template=MANIFEST_TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.substitute(version=version)


def stage_package(
    app_directory: Path,
    output: Path,
    version: str,
) -> None:
    """Copy the Nuitka standalone folder, logos and manifest into a new folder.
    Validate the folder before returning so MakeAppx only sees good input.
    """
    if not (app_directory / NUITKA_EXECUTABLE_NAME).is_file():
        raise MsixPackageError(
            f"{NUITKA_EXECUTABLE_NAME} does not exist in {app_directory}."
        )
    if output.exists():
        raise MsixPackageError(f"Package folder {output} already exists.")

    shutil.copytree(src=app_directory, dst=output)
    (output / NUITKA_EXECUTABLE_NAME).rename(target=output / EXECUTABLE_NAME)
    _copy_logos(output=output)
    (output / "AppxManifest.xml").write_text(
        data=render_manifest(version=version),
        encoding="utf-8",
    )
    validate_package(layout=output)


def _copy_logos(output: Path) -> None:
    # Windows paths ignore case, so the app's own assets folder and the
    # manifest's Assets folder are one folder in the package.
    for entry in output.iterdir():
        if entry.name.casefold() == LOGO_DIRECTORY_NAME.casefold():
            # Two renames, because Windows ignores a rename that only
            # changes case.
            staging = entry.with_name(name=f"{entry.name}.staging")
            entry.rename(target=staging)
            staging.rename(target=output / LOGO_DIRECTORY_NAME)
    logo_directory = output / LOGO_DIRECTORY_NAME
    logo_directory.mkdir(exist_ok=True)
    for logo in sorted(ASSETS_DIR.iterdir()):
        target = logo_directory / logo.name
        if target.exists():
            raise MsixPackageError(f"The app already has a file at {target}.")
        shutil.copy2(src=logo, dst=target)


def validate_package(layout: Path) -> None:
    """Check a staged package folder against the Store identity and rules.
    Raise MsixPackageError on the first problem found.
    """
    manifest_path = layout / "AppxManifest.xml"
    try:
        root = ElementTree.parse(source=manifest_path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise MsixPackageError(f"Cannot read {manifest_path}: {error}") from error

    _validate_unique_paths(layout=layout)
    _validate_identity(root=root)
    _validate_properties(root=root)
    _validate_device_family(root=root)
    application = _validate_application(root=root, layout=layout)
    _validate_capabilities(root=root)
    _validate_assets(root=root, application=application, layout=layout)


def _validate_unique_paths(layout: Path) -> None:
    # MakeAppx rejects two paths that differ only in case.
    seen: dict[str, str] = {}
    for path in sorted(layout.rglob(pattern="*")):
        relative = path.relative_to(layout).as_posix()
        previous = seen.setdefault(relative.casefold(), relative)
        if previous != relative:
            raise MsixPackageError(
                f"Package paths {previous} and {relative} differ only in case."
            )


def _validate_identity(root: ElementTree.Element) -> None:
    identity = _single(root=root, path=f"{{{FOUNDATION_NS}}}Identity")
    _expect(
        label="Identity Name",
        actual=identity.get(key="Name"),
        expected=IDENTITY_NAME,
    )
    _expect(
        label="Identity Publisher",
        actual=identity.get(key="Publisher"),
        expected=PUBLISHER,
    )
    _expect(
        label="ProcessorArchitecture",
        actual=identity.get(key="ProcessorArchitecture"),
        expected=ARCHITECTURE,
    )
    version = identity.get(key="Version") or ""
    parts = version.split(".")
    if len(parts) != 4 or parts[3] != "0":
        raise MsixPackageError(f"Identity Version {version!r} is not X.Y.Z.0.")
    parse_project_version(version=".".join(parts[:3]))


def _validate_properties(root: ElementTree.Element) -> None:
    properties = _single(root=root, path=f"{{{FOUNDATION_NS}}}Properties")
    _expect(
        label="DisplayName",
        actual=properties.findtext(path=f"{{{FOUNDATION_NS}}}DisplayName"),
        expected=DISPLAY_NAME,
    )
    _expect(
        label="PublisherDisplayName",
        actual=properties.findtext(path=f"{{{FOUNDATION_NS}}}PublisherDisplayName"),
        expected=PUBLISHER_DISPLAY_NAME,
    )


def _validate_device_family(root: ElementTree.Element) -> None:
    families = root.findall(
        path=f"{{{FOUNDATION_NS}}}Dependencies/{{{FOUNDATION_NS}}}TargetDeviceFamily"
    )
    if len(families) != 1:
        raise MsixPackageError(
            f"Expected one TargetDeviceFamily, found {len(families)}."
        )
    _expect(
        label="TargetDeviceFamily Name",
        actual=families[0].get(key="Name"),
        expected=DEVICE_FAMILY,
    )
    _expect(
        label="TargetDeviceFamily MinVersion",
        actual=families[0].get(key="MinVersion"),
        expected=MIN_VERSION,
    )


def _validate_application(
    root: ElementTree.Element,
    layout: Path,
) -> ElementTree.Element:
    applications = root.findall(
        path=f"{{{FOUNDATION_NS}}}Applications/{{{FOUNDATION_NS}}}Application"
    )
    if len(applications) != 1:
        raise MsixPackageError(
            f"Expected one Application, found {len(applications)}."
        )
    application = applications[0]
    _expect(
        label="Application Executable",
        actual=application.get(key="Executable"),
        expected=EXECUTABLE_NAME,
    )
    _expect(
        label="Application EntryPoint",
        actual=application.get(key="EntryPoint"),
        expected=FULL_TRUST_ENTRY_POINT,
    )
    if not (layout / EXECUTABLE_NAME).is_file():
        raise MsixPackageError(f"{EXECUTABLE_NAME} is missing from {layout}.")
    return application


def _validate_capabilities(root: ElementTree.Element) -> None:
    capabilities = _single(root=root, path=f"{{{FOUNDATION_NS}}}Capabilities")
    declared = set()
    for capability in capabilities:
        namespace = capability.tag.partition("}")[0].lstrip("{")
        declared.add((namespace, capability.get(key="Name")))
    if declared != REQUIRED_CAPABILITIES:
        names = sorted(str(name) for _, name in declared)
        raise MsixPackageError(
            f"Capabilities must be exactly runFullTrust, found {names}."
        )


def _validate_assets(
    root: ElementTree.Element,
    application: ElementTree.Element,
    layout: Path,
) -> None:
    visual = _single(root=application, path=f"{{{UAP_NS}}}VisualElements")
    tile = _single(root=visual, path=f"{{{UAP_NS}}}DefaultTile")
    references = {
        root.findtext(path=f"{{{FOUNDATION_NS}}}Properties/{{{FOUNDATION_NS}}}Logo"),
        visual.get(key="Square44x44Logo"),
        visual.get(key="Square150x150Logo"),
        tile.get(key="Wide310x150Logo"),
    }
    # Manifest paths use backslashes; compare them in POSIX form.
    referenced = {
        PureWindowsPath(reference).as_posix()
        for reference in references
        if reference is not None
    }
    if referenced != set(REQUIRED_ASSET_SIZES):
        raise MsixPackageError(
            f"Manifest logos {sorted(referenced)} do not match the required set "
            f"{sorted(REQUIRED_ASSET_SIZES)}."
        )
    for relative_path, expected_size in REQUIRED_ASSET_SIZES.items():
        actual_size = _png_size(path=layout / relative_path)
        if actual_size != expected_size:
            raise MsixPackageError(
                f"{relative_path} is {actual_size[0]}x{actual_size[1]}, "
                f"expected {expected_size[0]}x{expected_size[1]}."
            )


def _png_size(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as error:
        raise MsixPackageError(f"Cannot read logo {path}: {error}") from error
    if len(header) < 24 or not header.startswith(_PNG_SIGNATURE):
        raise MsixPackageError(f"Logo {path} is not a PNG file.")
    width = int.from_bytes(header[16:20], byteorder="big")
    height = int.from_bytes(header[20:24], byteorder="big")
    return width, height


def _single(root: ElementTree.Element, path: str) -> ElementTree.Element:
    matches = root.findall(path=path)
    if len(matches) != 1:
        name = path.rpartition("}")[2]
        raise MsixPackageError(f"Expected one {name}, found {len(matches)}.")
    return matches[0]


def _expect(label: str, actual: str | None, expected: str) -> None:
    if actual != expected:
        raise MsixPackageError(f"{label} is {actual!r}, expected {expected!r}.")


def build_parser() -> argparse.ArgumentParser:
    """Build the package staging argument parser.
    The CI job passes the previous release only when it builds a release tag.
    """
    parser = argparse.ArgumentParser(
        description="Stage and validate the Draft Omen MSIX package folder.",
    )
    parser.add_argument(
        "--app-directory",
        type=Path,
        required=True,
        help="Nuitka standalone folder that holds qt_gui.exe.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New package folder to create for MakeAppx.",
    )
    parser.add_argument(
        "--previous-version",
        default=None,
        help="Latest published release; the new version must be higher.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Stage the package folder and print its MSIX version.
    Return a nonzero status and explain invalid input on stderr.
    """
    args = build_parser().parse_args(args=argv)
    try:
        version = msix_version(
            project_version=read_project_version(),
            previous_version=args.previous_version,
        )
        stage_package(
            app_directory=args.app_directory,
            output=args.output,
            version=version,
        )
    except (MsixPackageError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
