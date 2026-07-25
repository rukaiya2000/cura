"""The armory — versioned skill storage, trust transitions and usage stats.

Layout on disk, one directory per version so a skill's history is never overwritten:

    armory/
      escalate_and_rebalance/
        v1/
          skill.py        the code
          manifest.json   the capability manifest
          test.py         the generated test that must pass to temper it
          stats.json      executions, successes, denials, timings

Trust only ever moves forward through evidence: a skill tempers when its generated test
passes, and becomes trusted after enough clean real executions. Nothing here lets a
caller simply declare a skill trustworthy.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import CapabilityManifest, Trust

#: Clean executions required before a tempered skill may run autonomously.
TRUST_THRESHOLD = 3

_SAFE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass
class SkillStats:
    executions: int = 0
    successes: int = 0
    denials: int = 0
    failures: int = 0
    durations_s: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successes / self.executions if self.executions else 0.0

    @property
    def mean_duration_s(self) -> float:
        return sum(self.durations_s) / len(self.durations_s) if self.durations_s else 0.0

    def to_dict(self) -> dict:
        d = {
            "executions": self.executions,
            "successes": self.successes,
            "denials": self.denials,
            "failures": self.failures,
            "durations_s": self.durations_s,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SkillStats:
        return cls(**d)


@dataclass
class Skill:
    manifest: CapabilityManifest
    source: str
    test_source: str = ""
    stats: SkillStats = field(default_factory=SkillStats)

    @property
    def name(self) -> str:
        return self.manifest.skill

    @property
    def version(self) -> int:
        return self.manifest.version

    @property
    def trust(self) -> Trust:
        return self.manifest.trust


class LibraryError(Exception):
    pass


class SkillLibrary:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths -------------------------------------------------------------

    def _dir(self, name: str, version: int) -> Path:
        if not _SAFE_NAME.match(name):
            raise LibraryError(f"unsafe skill name {name!r}")
        return self.root / name / f"v{version}"

    def versions(self, name: str) -> list[int]:
        base = self.root / name
        if not base.is_dir():
            return []
        return sorted(
            int(p.name[1:]) for p in base.iterdir()
            if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit()
        )

    def next_version(self, name: str) -> int:
        versions = self.versions(name)
        return (versions[-1] + 1) if versions else 1

    # --- read / write ------------------------------------------------------

    def register(self, skill: Skill, *, overwrite: bool = False) -> Skill:
        """Write a skill to disk. New skills always land quarantined."""
        d = self._dir(skill.name, skill.version)
        if d.exists() and not overwrite:
            raise LibraryError(f"{skill.manifest.qualified_name} already exists")
        d.mkdir(parents=True, exist_ok=True)
        (d / "skill.py").write_text(skill.source)
        (d / "test.py").write_text(skill.test_source)
        skill.manifest.write(d / "manifest.json")
        (d / "stats.json").write_text(json.dumps(skill.stats.to_dict(), indent=2) + "\n")
        return skill

    def load(self, name: str, version: int | None = None) -> Skill:
        version = version or (self.versions(name) or [0])[-1]
        d = self._dir(name, version)
        if not d.is_dir():
            raise LibraryError(f"no such skill {name}@v{version}")
        stats_path = d / "stats.json"
        stats = (SkillStats.from_dict(json.loads(stats_path.read_text()))
                 if stats_path.exists() else SkillStats())
        test_path = d / "test.py"
        return Skill(
            manifest=CapabilityManifest.read(d / "manifest.json"),
            source=(d / "skill.py").read_text(),
            test_source=test_path.read_text() if test_path.exists() else "",
            stats=stats,
        )

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def all_skills(self) -> list[Skill]:
        out = []
        for name in self.names():
            versions = self.versions(name)
            if versions:
                out.append(self.load(name, versions[-1]))
        return out

    def find(self, name: str) -> Skill | None:
        """Recognition before forging: reuse rather than re-invent."""
        return self.load(name) if self.versions(name) else None

    # --- trust -------------------------------------------------------------

    def _save_manifest(self, skill: Skill) -> None:
        skill.manifest.write(self._dir(skill.name, skill.version) / "manifest.json")

    def _save_stats(self, skill: Skill) -> None:
        path = self._dir(skill.name, skill.version) / "stats.json"
        path.write_text(json.dumps(skill.stats.to_dict(), indent=2) + "\n")

    def temper(self, skill: Skill) -> Skill:
        """Promote quarantined -> tempered. Only ever called on a passing test."""
        if skill.manifest.trust is Trust.QUARANTINED:
            skill.manifest.trust = Trust.TEMPERED
            self._save_manifest(skill)
        return skill

    def melt_down(self, skill: Skill) -> Skill:
        """Demote back to quarantine — the pruning path for a skill that keeps failing."""
        skill.manifest.trust = Trust.QUARANTINED
        self._save_manifest(skill)
        return skill

    def record_denial(self, skill: Skill) -> Skill:
        """A refusal that never reached execution.

        Kept separate from `record_execution` on purpose: the skill did not run, so
        counting it as an execution would distort success rate and mean duration. It is
        still worth recording — denials are the governance counterpart to executions,
        and a skill that is constantly refused is telling you something.
        """
        skill.stats.denials += 1
        self._save_stats(skill)
        return skill

    def record_execution(self, skill: Skill, *, ok: bool, denied: bool = False,
                         duration_s: float = 0.0) -> Skill:
        """Update stats and let trust follow the evidence."""
        s = skill.stats
        s.executions += 1
        s.durations_s.append(round(duration_s, 4))
        if denied:
            s.denials += 1
        elif ok:
            s.successes += 1
        else:
            s.failures += 1

        if (skill.manifest.trust is Trust.TEMPERED
                and s.successes >= TRUST_THRESHOLD
                and s.failures == 0):
            skill.manifest.trust = Trust.TRUSTED
            self._save_manifest(skill)

        self._save_stats(skill)
        return skill


def new_skill(manifest: CapabilityManifest, source: str, test_source: str = "") -> Skill:
    """Build an unregistered skill, forced into quarantine regardless of what was asked."""
    manifest.trust = Trust.QUARANTINED
    manifest.stats = {"forged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return Skill(manifest=manifest, source=source, test_source=test_source)
