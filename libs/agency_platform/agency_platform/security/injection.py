"""Prompt-injection check for text a user controls: their question, and text written inside a picture they sent.

Fast and free: weighted patterns (English, German, French, Spanish) → a score 0–1 and the names of what
matched. What happens next is config/guardrails.yaml → injection.mode (off / flag / block), and optionally a
cheap-model second opinion for borderline scores. Web pages are handled separately (fence_untrusted): they are
always treated as data.

This is a tripwire, not a wall: it catches the common phrasings and makes attempts visible in Admin → Guardrails.
The real protection stays structural — assistants never get tools that could leak other users' data, and
facts are verified against their sources.
"""
import re
from dataclasses import dataclass

_P = [
    # name, weight, pattern
    ("ignore_instructions", 0.7, r"\b(ignore|disregard|forget|skip|override)\b.{0,30}\b(previous|prior|above|earlier|all|your|the|any)\b"
                                 r".{0,20}\b(instructions?|prompts?|rules|directions|guidelines|guardrails)\b"),
    ("ignore_instructions", 0.7, r"\bignorier\w*\b.{0,30}\b(anweisungen|regeln|vorgaben)\b"),            # de
    ("ignore_instructions", 0.7, r"\bignore[rz]?\b.{0,30}\b(instructions|consignes|règles)\b"),           # fr
    ("ignore_instructions", 0.7, r"\bignora\w*\b.{0,30}\b(instrucciones|reglas)\b"),                      # es
    ("reveal_system_prompt", 0.7, r"\b(reveal|show|print|repeat|output|tell me|give me|leak)\b.{0,30}"
                                  r"\b(system|hidden|initial|original|secret)\s+(prompt|instructions?|message|rules)\b"),
    ("system_prompt_ref", 0.3, r"\bsystem\s*prompt\b"),
    ("role_override", 0.65, r"\b(you are now|from now on you are|act as|pretend (to be|you are)|roleplay as)\b"
                           r".{0,40}\b(unrestricted|unfiltered|jailbroken|evil|dan|developer mode|no (rules|limits))\b"),
    ("jailbreak_term", 0.5, r"\b(jailbreak|do anything now|developer mode enabled|DAN mode)\b"),
    ("fake_role_tag", 0.6, r"(<\s*/?\s*(system|assistant|instructions?)\s*>|\[/?(system|INST)\]|^\s*(system|assistant)\s*:)"),
    ("new_instructions", 0.4, r"\b(new|updated|real|actual)\s+instructions?\s*:"),
    ("exfiltrate", 0.5, r"\b(send|post|upload|exfiltrate)\b.{0,40}\b(api key|password|token|secret|credentials)\b"),
]
_COMPILED = [(n, w, re.compile(p, re.IGNORECASE | re.MULTILINE)) for n, w, p in _P]


@dataclass(frozen=True)
class InjectionCheck:
    score: float
    matches: tuple[str, ...]

    def is_attempt(self, threshold: float) -> bool:
        return self.score >= threshold

    @property
    def detail(self) -> str:
        """For the guardrail log: pattern names only, never the text."""
        return "patterns: " + ", ".join(self.matches) if self.matches else ""


def check(text: str) -> InjectionCheck:
    """Pure: 1 - Π(1 - weight) over the distinct patterns that match (two medium hits ≈ one strong)."""
    names: dict[str, float] = {}
    for name, weight, rx in _COMPILED:
        if name not in names and rx.search(text):
            names[name] = weight
    remaining = 1.0
    for w in names.values():
        remaining *= 1 - w
    return InjectionCheck(round(1 - remaining, 3), tuple(names))
