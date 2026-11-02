"""Dependencies whose absence is silent.

The 1.9.69 image installed ``pymupdf4llm`` 2.0, a release that ships no module
and pulls in no PyMuPDF, so the pymupdf4llm PDF backend was skipped in
production without a word, and anything needing PyMuPDF could not run. PyMuPDF
is now a direct dependency (marked-up extraction PDFs need it) and pymupdf4llm
is capped below 2."""
import importlib.metadata as md

import pytest


def test_pymupdf_is_importable():
    import pymupdf
    assert tuple(int(p) for p in pymupdf.VersionBind.split(".")[:2]) >= (1, 24)


def test_pymupdf4llm_if_installed_is_a_real_release():
    try:
        version = md.version("pymupdf4llm")
    except md.PackageNotFoundError:
        pytest.skip("pymupdf4llm not installed (optional backend)")
    assert int(version.split(".")[0]) < 2, f"pymupdf4llm {version} is an empty shim release"
    import pymupdf4llm  # noqa: F401  (the module must actually exist)


def test_yaml_is_available_for_prompt_set_export():
    import yaml
    assert yaml.safe_dump({"a": 1}).strip() == "a: 1"
