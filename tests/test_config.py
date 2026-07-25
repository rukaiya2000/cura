"""The .env loader.

Small module, but the failure modes are the quiet kind: a template line shadowing a real
credential, or a checked-in file overriding what CI exported.
"""

import os

from skillforge.config import CAPABILITIES, get, load_env


def write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_loads_simple_pairs(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_TEST_KEY", raising=False)
    applied = load_env(write(tmp_path, "FORGE_TEST_KEY=abc123\n"))

    assert applied == {"FORGE_TEST_KEY": "abc123"}
    assert os.environ["FORGE_TEST_KEY"] == "abc123"


def test_ignores_comments_blanks_and_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_A", raising=False)
    applied = load_env(write(tmp_path, "\n".join([
        "# a comment",
        "",
        "   ",
        "not-a-pair",
        "FORGE_A=1",
    ])))
    assert applied == {"FORGE_A": "1"}


def test_strips_quotes_and_an_export_prefix(tmp_path, monkeypatch):
    for key in ("FORGE_Q", "FORGE_S", "FORGE_E"):
        monkeypatch.delenv(key, raising=False)
    applied = load_env(write(tmp_path, "\n".join([
        'FORGE_Q="double quoted"',
        "FORGE_S='single quoted'",
        "export FORGE_E=exported",
    ])))
    assert applied["FORGE_Q"] == "double quoted"
    assert applied["FORGE_S"] == "single quoted"
    assert applied["FORGE_E"] == "exported"


def test_a_blank_template_line_does_not_shadow_a_real_variable(tmp_path, monkeypatch):
    """The failure this guards: an unfilled `KEY=` looking configured and failing oddly."""
    monkeypatch.setenv("FORGE_REAL", "from-the-environment")
    load_env(write(tmp_path, "FORGE_REAL=\n"))

    assert os.environ["FORGE_REAL"] == "from-the-environment"
    assert get("FORGE_REAL") == "from-the-environment"


def test_a_blank_value_reads_as_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_EMPTY", "")
    assert get("FORGE_EMPTY") is None
    assert get("FORGE_EMPTY", "fallback") == "fallback"


def test_the_real_environment_wins_over_the_file(tmp_path, monkeypatch):
    """CI and production export credentials; a checked-in file must not shadow them."""
    monkeypatch.setenv("FORGE_PRIORITY", "exported")
    applied = load_env(write(tmp_path, "FORGE_PRIORITY=from-file\n"))

    assert applied == {}
    assert os.environ["FORGE_PRIORITY"] == "exported"


def test_override_is_available_but_not_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_PRIORITY", "exported")
    load_env(write(tmp_path, "FORGE_PRIORITY=from-file\n"), override=True)
    assert os.environ["FORGE_PRIORITY"] == "from-file"


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "nope.env") == {}


def test_every_capability_names_a_fallback():
    """No credential is required to run the project — each gap must have a stated
    fallback, or the report would be telling the user something untrue."""
    for cap in CAPABILITIES:
        assert cap.fallback, f"{cap.name} claims no fallback"
        assert cap.required, f"{cap.name} requires nothing"


def test_the_env_example_documents_every_key_the_capabilities_reference():
    """A key the report checks but the template never mentions is unfillable."""
    from skillforge.config import ROOT

    template = (ROOT / ".env.example").read_text()
    blank = (ROOT / ".env").read_text()
    for cap in CAPABILITIES:
        for key in cap.required + cap.optional:
            assert f"{key}=" in template, f"{key} missing from .env.example"
            assert f"{key}=" in blank, f"{key} missing from .env"


def test_the_env_file_is_gitignored():
    """The one mistake here that cannot be undone."""
    from skillforge.config import ROOT

    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    assert "!.env.example" in ignored, "the template must stay committable"
