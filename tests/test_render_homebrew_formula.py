import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.render_homebrew_formula import (
    SourceDistribution,
    render_formula,
    resolve_source_distribution,
)

SOURCE_URL = (
    "https://files.pythonhosted.org/packages/example/draftomen-0.3.0.tar.gz"
)
SOURCE_SHA256 = "a" * 64


def test_resolve_source_distribution_returns_the_unique_sdist() -> None:
    release_metadata = {
        "info": {"version": "0.3.0"},
        "urls": [
            {
                "packagetype": "bdist_wheel",
                "url": "https://files.pythonhosted.org/example.whl",
                "digests": {"sha256": "b" * 64},
            },
            {
                "packagetype": "sdist",
                "url": SOURCE_URL,
                "digests": {"sha256": SOURCE_SHA256},
            },
        ],
    }

    source_distribution = resolve_source_distribution(
        release_metadata=release_metadata,
        version="0.3.0",
    )

    assert source_distribution == SourceDistribution(
        url=SOURCE_URL,
        sha256=SOURCE_SHA256,
    )


@pytest.mark.parametrize(
    ("release_metadata", "expected_message"),
    [
        (
            {"info": {"version": "0.2.0"}, "urls": []},
            "does not match the requested version",
        ),
        (
            {"info": {"version": "0.3.0"}, "urls": []},
            "Expected one PyPI source distribution, found 0",
        ),
        (
            {
                "info": {"version": "0.3.0"},
                "urls": [
                    {
                        "packagetype": "sdist",
                        "url": "https://example.com/draftomen.tar.gz",
                        "digests": {"sha256": SOURCE_SHA256},
                    }
                ],
            },
            "unexpected URL",
        ),
    ],
)
def test_resolve_source_distribution_rejects_invalid_metadata(
    release_metadata: dict[str, object],
    expected_message: str,
) -> None:
    with pytest.raises(RuntimeError, match=expected_message):
        resolve_source_distribution(
            release_metadata=release_metadata,
            version="0.3.0",
        )


def test_render_formula_replaces_source_placeholders(tmp_path: Path) -> None:
    template_path = tmp_path / "draftomen.rb.in"
    output_path = tmp_path / "draftomen.rb"
    template_path.write_text(
        data="url \"$PYPI_URL\"\nsha256 \"$PYPI_SHA256\"\n",
        encoding="utf-8",
    )

    render_formula(
        template_path=template_path,
        output_path=output_path,
        source_distribution=SourceDistribution(
            url=SOURCE_URL,
            sha256=SOURCE_SHA256,
        ),
    )

    assert output_path.read_text(encoding="utf-8") == (
        f"url \"{SOURCE_URL}\"\nsha256 \"{SOURCE_SHA256}\"\n"
    )


def test_render_formula_preserves_homebrew_pyside_dependency_contract(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "draftomen.rb"

    render_formula(
        template_path=PROJECT_ROOT / ".github/homebrew/draftomen.rb.in",
        output_path=output_path,
        source_distribution=SourceDistribution(
            url=SOURCE_URL,
            sha256=SOURCE_SHA256,
        ),
    )

    rendered_formula = output_path.read_text(encoding="utf-8")
    assert 'depends_on "pyside"' in rendered_formula
    assert 'depends_on "rust" => :build' in rendered_formula
    assert (
        'pypi_packages package_name:     "draftomen",\n'
        '                exclude_packages: ["pillow", "pyside6"]'
    ) in rendered_formula
