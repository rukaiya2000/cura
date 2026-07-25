import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions  # noqa: E402
from skillforge.core.library import Skill, SkillLibrary, new_skill  # noqa: E402
from skillforge.core.manifest import CapabilityManifest  # noqa: E402

SEEDS = ROOT / "seeds"


@pytest.fixture
def actions():
    return FakeScalekitActions()


@pytest.fixture
def client_for(actions):
    def _make(identifier, *, dry_run=False):
        return BoundScopedClient(actions, identifier, dry_run=dry_run)
    return _make


@pytest.fixture
def library(tmp_path):
    return SkillLibrary(tmp_path / "armory")


def load_seed(name: str) -> Skill:
    d = SEEDS / name
    manifest = CapabilityManifest.from_dict(json.loads((d / "manifest.json").read_text()))
    return new_skill(
        manifest,
        source=(d / "skill.py").read_text(),
        test_source=(d / "test.py").read_text(),
    )


@pytest.fixture
def escalate_skill():
    return load_seed("escalate_and_rebalance")
