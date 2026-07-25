"""Admin ceilings — what the forge may *ever* produce, regardless of who asks.

The gates in `router.py` are derived from the skill and the speaker: does this person hold
these primitives, has this skill earned autonomy. Policy is the layer above that, and it
answers a different question — one no individual grant can override:

    "Nobody in this org gets a destructive Linear skill, even if they're an admin."

Two hooks, and the order matters more than either rule:

    filter_primitives()   BEFORE generation — the model is never shown what policy bans
    evaluate()            BEFORE registration and again before execution

Filtering first is the same trick the scope ceiling uses: a capability the generator
cannot see is a capability it cannot compose, so the common case never becomes a refusal
at all. `evaluate()` is the backstop for everything filtering can't express — a primitive
count, a reversibility requirement, an effect class the model inferred wrongly.

A `Policy()` with no arguments permits everything. That is deliberate: policy is opt-in,
and an empty policy must not silently become a deny-all that looks like a broken forge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .manifest import CapabilityManifest, Effect


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class Policy:
    """An admin's ceiling, evaluated over manifests rather than over prose.

    Every field is a *ceiling*, never a grant: policy can only ever narrow what a
    speaker's own permissions already allow. There is no field here that lets policy
    hand someone a capability Scalekit didn't give them, and that asymmetry is the point.
    """

    #: Effect classes nobody may forge or run. `{Effect.DESTRUCTIVE}` is the common one.
    deny_effects: frozenset[Effect] = frozenset()

    #: Apps that are off-limits entirely.
    deny_apps: frozenset[str] = frozenset()

    #: If set, the *only* apps permitted. An empty frozenset denies every app; `None`
    #: means "no allowlist configured" — the two are different and easy to confuse.
    allow_apps: frozenset[str] | None = None

    #: Specific primitives banned regardless of who holds them.
    deny_primitives: frozenset[str] = frozenset()

    #: A ceiling on composition size. A skill touching forty primitives is not a skill,
    #: it's a script, and it should be reviewed rather than forged.
    max_primitives: int | None = None

    #: Effect classes that may only be forged if the skill declares an inverse.
    require_reversible: frozenset[Effect] = frozenset()

    #: Effect classes that always need a human, on top of the trust ladder.
    always_confirm: frozenset[Effect] = frozenset()

    label: str = "default"

    # --- hook 1: before generation ----------------------------------------

    def filter_primitives(self, tools: list[dict]) -> list[dict]:
        """Drop primitives the policy bans from what the generator is shown.

        Runs before generation so a banned capability is never composed in the first
        place. The result is that policy usually costs nothing at runtime — there is no
        refusal because there was never an attempt.
        """
        return [t for t in tools if self.permits_primitive(t["definition"]["name"])]

    def constraints(self) -> list[str]:
        """The manifest-level ceilings, phrased for whoever is writing the skill.

        `filter_primitives` already removes banned capabilities before generation, so
        policy costs nothing at runtime for *those*. The manifest rules had no equivalent:
        they were enforced only after the model had written something, which is how a live
        run burned three attempts oscillating between `reversible: true, inverse: null`
        (rejected by the manifest) and `reversible: false` (rejected by this policy). Both
        rejections were correct and neither was knowable in advance. Now they are stated
        up front, so the common case is that no refusal happens because no attempt did.
        """
        out: list[str] = []
        if self.deny_effects:
            banned = ", ".join(sorted(e.value for e in self.deny_effects))
            out.append(f"Do not write a skill whose effects are: {banned}.")
        if self.require_reversible:
            need = ", ".join(sorted(e.value for e in self.require_reversible))
            out.append(
                f"A skill with effects {need} must set `reversible` true and name its "
                "inverse skill in `inverse`. If the action genuinely cannot be undone, "
                "this policy will not permit it at all."
            )
        if self.max_primitives is not None:
            out.append(f"Use at most {self.max_primitives} primitives.")
        return out

    def permits_primitive(self, primitive: str) -> bool:
        if primitive in self.deny_primitives:
            return False
        app = primitive.split(".", 1)[0]
        if app in self.deny_apps:
            return False
        if self.allow_apps is not None and app not in self.allow_apps:
            return False
        return True

    # --- hook 2: before registration and before execution -----------------

    def evaluate(self, manifest: CapabilityManifest) -> Verdict:
        """Check a manifest against the ceiling. Collects *all* reasons, not the first.

        All of them because a caller fixing one violation only to hit the next learns
        nothing on each round trip — and because a generator regenerating from feedback
        does better with the full list.
        """
        reasons: list[str] = []

        if manifest.effects in self.deny_effects:
            reasons.append(
                f"policy '{self.label}' forbids {manifest.effects.value} skills"
            )

        # Set intersections, now that a skill can span apps. A multi-app skill is refused
        # if *any* of its apps is denied — a ceiling that held for three of four services
        # would not be a ceiling.
        forbidden = sorted(set(manifest.apps) & self.deny_apps)
        if forbidden:
            reasons.append(
                f"policy '{self.label}' forbids acting on {', '.join(forbidden)}"
            )
        if self.allow_apps is not None:
            outside = sorted(set(manifest.apps) - self.allow_apps)
            if outside:
                permitted = ", ".join(sorted(self.allow_apps)) or "nothing"
                reasons.append(
                    f"policy '{self.label}' permits only {permitted}, "
                    f"not {', '.join(outside)}"
                )

        banned = sorted(set(manifest.primitives_used) & self.deny_primitives)
        if banned:
            reasons.append(f"policy '{self.label}' forbids {', '.join(banned)}")

        if self.max_primitives is not None and len(manifest.primitives_used) > self.max_primitives:
            reasons.append(
                f"policy '{self.label}' caps a skill at {self.max_primitives} primitives; "
                f"this one declares {len(manifest.primitives_used)}"
            )

        if manifest.effects in self.require_reversible and not manifest.reversible:
            reasons.append(
                f"policy '{self.label}' requires {manifest.effects.value} skills to "
                "declare an inverse"
            )

        return Verdict(allowed=not reasons, reasons=tuple(reasons))

    def needs_confirmation(self, manifest: CapabilityManifest) -> bool:
        """Policy's own confirmation requirement, on top of the trust ladder's."""
        return manifest.effects in self.always_confirm


#: A reasonable starting ceiling for a real deployment: nothing destructive, nothing
#: outside Linear, no oversized compositions, and writes must be undoable. Offered as a
#: worked example — `Policy()` (permits everything) stays the default so an unconfigured
#: forge behaves predictably rather than mysteriously.
STRICT = Policy(
    label="strict",
    deny_effects=frozenset({Effect.DESTRUCTIVE}),
    allow_apps=frozenset({"linear"}),
    deny_primitives=frozenset({"linear.delete_project"}),
    max_primitives=10,
    require_reversible=frozenset({Effect.WRITE}),
    always_confirm=frozenset({Effect.DESTRUCTIVE}),
)
