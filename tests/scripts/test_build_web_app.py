import base64
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def build_web_app():
    spec = importlib.util.spec_from_file_location(
        "build_web_app", PROJECT_ROOT / "scripts" / "build_web_app.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["build_web_app"] = module
    spec.loader.exec_module(module)
    return module


def _make_dummy_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("dummy/__init__.py", "")
    return path


def test_dependencies_are_split_into_pypi_and_git(build_web_app):
    pypi, git, version = build_web_app.collect_dependencies(PROJECT_ROOT / "pyproject.toml")
    assert version
    assert any(req.startswith("pydantic") for req in pypi)
    assert any(req.startswith("reportlab") for req in pypi)
    assert all("git+" not in req for req in pypi)
    joined = " ".join(git)
    assert "ibflex2" in joined and "pdf417gen" in joined
    # Inline TOML comments must not leak into requirement strings.
    assert all("#" not in req for req in pypi)


def test_bundle_embeds_wheels_and_identifiers(build_web_app, tmp_path):
    wheel = _make_dummy_wheel(tmp_path / "dummy-1.0-py3-none-any.whl")
    csv = tmp_path / "ids.csv"
    csv.write_text("isin,valor\n")
    bundle = build_web_app.make_bundle(
        [wheel], ["pydantic"], {"typer": "0.24.2"}, "1.2.3", identifiers_csv=csv
    )
    assert bundle["version"] == "1.2.3"
    assert bundle["pyodidePackages"] == ["pydantic"]
    assert bundle["lock"] == {"typer": "0.24.2"}
    assert bundle["wheels"][0]["name"] == "dummy-1.0-py3-none-any.whl"
    assert base64.b64decode(bundle["wheels"][0]["data"]) == wheel.read_bytes()
    assert base64.b64decode(bundle["identifiersCsv"]) == b"isin,valor\n"


def test_locked_closure_splits_dist_and_embedded_packages(build_web_app):
    pypi, git, _ = build_web_app.collect_dependencies(PROJECT_ROOT / "pyproject.toml")
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    skip = {canonicalize_name(Requirement(req).name) for req in git}
    skip.add(canonicalize_name("opensteuerauszug"))
    # Pretend the Pyodide distribution ships the binary packages (as the real
    # lockfile does) so the walk routes them there instead of embedding.
    dist = {name: name for name in ("lxml", "pillow", "pydantic", "click", "rich")}
    locked, dist_used = build_web_app.resolve_locked_closure(pypi, skip, dist)
    assert "lxml" in dist_used and "pydantic" in dist_used
    # Direct deps and transitive deps of embedded wheels get exact pins…
    import typer

    assert locked["typer"] == typer.__version__
    assert "reportlab" in locked and "shellingham" in locked
    # …and nothing routed to the distribution is also embedded.
    assert not set(locked) & set(dist_used)


def test_rendered_html_contains_parseable_bundle_json(build_web_app, tmp_path):
    template = (PROJECT_ROOT / "web" / "app_template.html").read_text(encoding="utf-8")
    wheel = _make_dummy_wheel(tmp_path / "dummy-1.0-py3-none-any.whl")
    bundle = build_web_app.make_bundle(
        [wheel], ["pydantic"], {}, "1.2.3", identifiers_csv=tmp_path / "missing.csv"
    )
    # A hostile-looking string must not be able to close the <script> tag.
    bundle["pyodidePackages"].append("evil</script><script>")
    html = build_web_app.render_html(template, bundle)
    assert build_web_app.BUNDLE_PLACEHOLDER not in html
    assert "</script><script>alert" not in html
    start = html.index('<script type="application/json" id="osa-bundle">') + len(
        '<script type="application/json" id="osa-bundle">'
    )
    end = html.index("</script>", start)
    parsed = json.loads(html[start:end])
    assert parsed["version"] == "1.2.3"
    assert parsed["wheels"][0]["name"] == "dummy-1.0-py3-none-any.whl"
    assert "</script>" not in html[start:end]


def test_render_html_rejects_template_without_placeholder(build_web_app):
    with pytest.raises(ValueError):
        build_web_app.render_html("<html></html>", {"version": "1"})


def test_pyodide_lock_tables(build_web_app):
    packages = {
        "pydantic": {"version": "2.12.5"},
        "Pillow": {"version": "11.3.0"},
        "micropip": {},
    }
    assert build_web_app.pyodide_distribution(packages)["pillow"] == "Pillow"
    versions = build_web_app.pyodide_versions(packages)
    assert versions == {"pydantic": "2.12.5", "pillow": "11.3.0"}


def test_locked_closure_rejects_requirements_pyodide_cannot_meet(build_web_app):
    # The installed pydantic-xml declares "pydantic>=2.6.0,!=2.10.0b1"; a
    # Pyodide pydantic below that must stop the build with a clear message
    # instead of producing a page that fails at import time in the browser.
    dist = {"pydantic": "pydantic", "pydantic-core": "pydantic_core", "lxml": "lxml"}
    with pytest.raises(SystemExit) as excinfo:
        build_web_app.resolve_locked_closure(["pydantic-xml"], set(), dist, {"pydantic": "2.5.0"})
    message = str(excinfo.value)
    assert "pydantic-xml" in message and "Pyodide ships pydantic 2.5.0" in message
    assert "uv sync --locked" in message


def test_locked_closure_accepts_compatible_pyodide_versions(build_web_app):
    import pydantic

    dist = {"pydantic": "pydantic", "pydantic-core": "pydantic_core", "lxml": "lxml"}
    locked, dist_used = build_web_app.resolve_locked_closure(
        ["pydantic-xml"], set(), dist, {"pydantic": pydantic.VERSION}
    )
    assert "pydantic-xml" in locked
    assert "pydantic" in dist_used


def test_locked_closure_only_notes_our_own_floors(build_web_app, capsys):
    # pyproject pins like "requests>=2.33.0" are CLI minimums; Pyodide's older
    # requests still imports fine, so the build reports it and carries on.
    dist = {"requests": "requests"}
    _, dist_used = build_web_app.resolve_locked_closure(
        ["requests>=2.33.0"], set(), dist, {"requests": "2.32.4"}
    )
    assert dist_used == ["requests"]
    assert "Pyodide ships requests 2.32.4" in capsys.readouterr().out
