"""Alphabet guard for validating characters against allowed alphabet Sigma."""

from __future__ import annotations

import string
from dataclasses import dataclass
from .models import SigmaPolicy


DEFAULT_SIGMA: set[str] = set(
    string.ascii_letters
    + string.digits
    + string.punctuation
    + " \t\n\r"
)


@dataclass(slots=True)
class SigmaValidationResult:
    """Result of validating text against alphabet Sigma."""

    is_valid: bool
    violations: list[str]
    policy: SigmaPolicy
    is_blocked: bool = False
    warning: str | None = None


IGNORABLE_FORMATTING_CHARS = {
    "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff", "\u200b",
}


class SigmaGuard:
    """Protects against queries containing symbols outside the system alphabet Sigma."""

    def __init__(
        self,
        allowed_alphabet: set[str] | str | None = None,
        default_policy: SigmaPolicy = SigmaPolicy.WARN,
    ) -> None:
        self.alphabet = set(allowed_alphabet) if allowed_alphabet is not None else set(DEFAULT_SIGMA)
        self.policy = default_policy

    def find_violations(self, text: str) -> list[str]:
        """Return a sorted list of unique characters in text outside alphabet Sigma."""
        return sorted({ch for ch in text if ch not in self.alphabet and ch not in IGNORABLE_FORMATTING_CHARS})

    def validate(
        self,
        text: str,
        policy: SigmaPolicy | None = None,
    ) -> SigmaValidationResult:
        """Validate text against Sigma under the specified policy."""
        active_policy = policy if policy is not None else self.policy
        if active_policy is SigmaPolicy.OFF:
            return SigmaValidationResult(
                is_valid=True,
                violations=[],
                policy=active_policy,
                is_blocked=False,
                warning=None,
            )

        violations = self.find_violations(text)
        if not violations:
            return SigmaValidationResult(
                is_valid=True,
                violations=[],
                policy=active_policy,
                is_blocked=False,
                warning=None,
            )

        preview = " ".join(f"'{ch}' (U+{ord(ch):04X})" for ch in violations[:5])
        if len(violations) > 5:
            preview += f" ... (+{len(violations) - 5} more)"

        if active_policy is SigmaPolicy.BLOCK:
            return SigmaValidationResult(
                is_valid=False,
                violations=violations,
                policy=active_policy,
                is_blocked=True,
                warning=(
                    f"Blocked query containing symbols outside allowed alphabet Sigma: {preview}"
                ),
            )

        # WARN policy
        return SigmaValidationResult(
            is_valid=False,
            violations=violations,
            policy=active_policy,
            is_blocked=False,
            warning=(
                f"Warning: query contains symbols outside allowed alphabet Sigma: {preview}"
            ),
        )
