"""The .env loader.

Small module, but the failure modes are the quiet kind: a template line shadowing a real
credential, or a checked-in file overriding what CI exported.
"""

import os

import pytest

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
    """A key the report checks but the template never mentions is unfillable.

    Compares *key names only*. An earlier version asserted `key in text` against the
    whole file, and pytest renders the compared value on failure — so one drifted key
    printed the developer's real `.env`, secrets and all, into the test output. Reducing
    both files to their key names first means a failure here can only ever show names.
    """
    from skillforge.config import ROOT

    def key_names(path):
        return {line.split("=", 1)[0].strip()
                for line in path.read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#")}

    template = key_names(ROOT / ".env.example")
    blank = key_names(ROOT / ".env")
    for cap in CAPABILITIES:
        for key in cap.required + cap.optional:
            assert key in template, f"{key} missing from .env.example"
            assert key in blank, f"{key} missing from .env"


def test_the_env_file_is_gitignored():
    """The one mistake here that cannot be undone."""
    from skillforge.config import ROOT

    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    assert "!.env.example" in ignored, "the template must stay committable"


# --- the report must keep its own promise -----------------------------------


def test_the_report_never_prints_a_secret_value(monkeypatch):
    """It prints "values are never printed" at the top and then printed a live webhook
    secret in full, because optional keys showed their value and one of them was a
    secret. The header was true of required keys and false of optional ones."""
    from skillforge.config import report

    planted = {
        "MEETSTREAM_WEBHOOK_SECRET": "sk-live-DO-NOT-PRINT-4a3e4a6b",
        "ANTHROPIC_API_KEY": "sk-ant-DO-NOT-PRINT-9912",
        "SCALEKIT_CLIENT_SECRET": "scs-DO-NOT-PRINT-7719",
    }
    for key, value in planted.items():
        monkeypatch.setenv(key, value)

    text = report()
    for key, value in planted.items():
        assert value not in text, f"{key} was printed in full"
        assert key in text, f"{key} should still be listed as set"


def test_a_non_secret_optional_value_is_still_shown():
    """Masking everything would be safe and useless — SKILLFORGE_CONNECTIONS=gmail is
    exactly the value you open this report to check."""
    from skillforge.config import is_secret

    assert not is_secret("SKILLFORGE_CONNECTIONS")
    assert not is_secret("MEETSTREAM_BOT_NAME")
    assert not is_secret("SKILLFORGE_IDENTIFIER_FULL")


@pytest.mark.parametrize("key", [
    "MEETSTREAM_WEBHOOK_SECRET", "ANTHROPIC_API_KEY", "SCALEKIT_CLIENT_SECRET",
    "SOME_ACCESS_TOKEN", "DB_PASSWORD", "GCP_CREDENTIAL",
])
def test_anything_named_like_a_secret_is_treated_as_one(key):
    from skillforge.config import is_secret

    assert is_secret(key)
