"""The capability manifest — the keystone that joins the forge to governance.

A forged skill is never just code. It is code + tests + a manifest declaring exactly
what the skill is allowed to touch. Everything downstream reads the manifest rather
than reading prose or guessing from the code:

  * the static checker reconciles declared primitives against the code's actual calls
  * the policy engine evaluates admin ceilings over typed fields, not strings
  * quarantine is mechanical: trust != TRUSTED means dry-run only
  * the audit log stores a structured object
  * undo is declared (`inverse`) rather than inferred
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path


class Effect(str, Enum):
    """What a skill does to the world. Ordered: each level implies the ones before it."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def rank(self) -> int:
        return {"read": 0, "write": 1, "destructive": 2}[self.value]


class Trust(str, Enum):
    """How much rope a skill has earned.

    QUARANTINED -> freshly forged; dry-run only, never touches the real world.
    TEMPERED    -> its generated test passed; may execute with confirmation.
    TRUSTED     -> enough clean real executions; may execute autonomously.
    """

    QUARANTINED = "quarantined"
    TEMPERED = "tempered"
    TRUSTED = "trusted"


class ManifestError(ValueError):
    """The manifest is malformed or internally inconsistent."""


@dataclass
class CapabilityManifest:
    skill: str
    #: Every app this skill may touch. A list rather than a single string because the
    #: skills worth forging span services — "book the follow-up *and* log it to the
    #: record" is one intent reaching Calendar and HubSpot, and a single-app field
    #: rejected it outright. The host sets this; a skill never names its own apps.
    apps: list[str]
    primitives_used: list[str]
    effects: Effect
    version: int = 1
    reversible: bool = False
    inverse: str | None = None
    forged_by: str | None = None  # the maker's mark
    trust: Trust = Trust.QUARANTINED
    description: str = ""
    stats: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept plain strings so manifests can be loaded straight from JSON.
        if isinstance(self.effects, str):
            self.effects = Effect(self.effects)
        if isinstance(self.trust, str):
            self.trust = Trust(self.trust)
        if isinstance(self.apps, str):        # a single app needs no ceremony
            self.apps = [self.apps]
        self.apps = list(dict.fromkeys(self.apps))    # dedupe, keep declared order
        self.validate()

    def validate(self) -> None:
        if not self.skill or not self.skill.isidentifier():
            raise ManifestError(f"skill name must be a valid identifier, got {self.skill!r}")
        if not self.apps or not all(self.apps):
            raise ManifestError("manifest must name the app(s) it acts on")
        if not self.primitives_used:
            raise ManifestError("a skill that declares no primitives cannot do anything")

        declared = set(self.apps)
        for p in self.primitives_used:
            if "." not in p:
                raise ManifestError(f"primitive must be namespaced as 'app.action', got {p!r}")
            # Still a hard boundary — widening `app` to `apps` lets a skill span services
            # it *declared*, and changes nothing about one it did not. Reaching an
            # undeclared app remains the failure it always was.
            if p.split(".", 1)[0] not in declared:
                raise ManifestError(
                    f"primitive {p!r} is outside the declared apps "
                    f"{', '.join(self.apps)!r}; "
                    "a skill may not reach across apps without declaring it"
                )
        if self.reversible and not self.inverse:
            raise ManifestError("reversible skills must name their inverse")
        if self.inverse and not self.reversible:
            raise ManifestError(f"{self.skill!r} names an inverse but is not marked reversible")
        if self.version < 1:
            raise ManifestError("version starts at 1")

    @property
    def qualified_name(self) -> str:
        return f"{self.skill}@v{self.version}"

    @property
    def may_execute_for_real(self) -> bool:
        """Quarantine, mechanically. Nothing untempered touches the real world."""
        return self.trust in (Trust.TEMPERED, Trust.TRUSTED)

    @property
    def needs_confirmation(self) -> bool:
        """Tempered-but-not-trusted skills, and anything destructive, get a human."""
        return self.trust is not Trust.TRUSTED or self.effects is Effect.DESTRUCTIVE

    def to_dict(self) -> dict:
        d = asdict(self)
        d["effects"] = self.effects.value
        d["trust"] = self.trust.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CapabilityManifest:
        # Migration shim: manifests written before skills could span apps carry a single
        # `app` string. They are on disk in armory/ and must keep loading, so translate
        # rather than making the reader's problem the user's problem.
        if "app" in d and "apps" not in d:
            d = {k: v for k, v in d.items() if k != "app"} | {"apps": d["app"]}

        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"unknown manifest fields: {sorted(unknown)}")
        return cls(**d)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> CapabilityManifest:
        return cls.from_dict(json.loads(path.read_text()))
