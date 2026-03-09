from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .trust_db import HardConstraint


@dataclass(frozen=True)
class ParseResult:
    constraints: list[HardConstraint]
    behavioral_guidelines: list[str]
    unresolved_lines: list[str]


_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.\-*]*|[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+"
)

_DENY_KEYWORDS = (
    "do not modify",
    "don't modify",
    "never modify",
    "must not modify",
    "read-only",
)
_CHECK_IN_KEYWORDS = (
    "always check in",
    "always ask",
    "require approval",
    "be careful with",
    "check in for",
)
_ALLOW_KEYWORDS = (
    "always allow",
    "always_allow",
    "trusted",
    "edit freely",
)
_READ_ALLOW_KEYWORDS = (
    "allow reads",
    "allow read",
    "read freely",
    "read access is allowed",
    "inspect freely",
)
_READ_DENY_KEYWORDS = (
    "do not read",
    "don't read",
    "never read",
    "must not read",
)
_READ_CHECKIN_KEYWORDS = (
    "check in before reading",
    "ask before reading",
    "approval before reading",
)
_WRITE_ALLOW_KEYWORDS = (
    "allow writes",
    "allow write",
    "edit freely",
    "write access is allowed",
)
_WRITE_DENY_KEYWORDS = (
    "do not write",
    "don't write",
    "never write",
    "must not write",
)
_WRITE_CHECKIN_KEYWORDS = (
    "check in before writing",
    "ask before writing",
    "approval before writing",
)
_GUIDELINE_KEYWORDS = (
    "always ",
    "do not ",
    "don't ",
    "never ",
    "must ",
    "should ",
    "prefer ",
    "use ",
    "avoid ",
    "follow ",
    "run tests",
    "be careful",
)


def _normalize_pattern(token: str) -> str:
    token = token.strip().strip(".,:;")
    if token.startswith("./"):
        token = token[2:]
    if token.endswith("/"):
        token = token + "*"
    token = str(Path(token))
    return token


def _extract_path_tokens(line: str) -> list[str]:
    tokens: list[str] = []
    for match in re.findall(r"`([^`]+)`", line):
        tokens.append(match)
    for match in _PATH_TOKEN_RE.findall(line):
        tokens.append(match)
    filtered: list[str] = []
    for token in tokens:
        if "://" in token:
            continue
        if token.startswith("-"):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)+", token):
            continue
        if re.fullmatch(r"\d+/\d+", token):
            continue
        if not any(ch.isalpha() for ch in token):
            continue
        normalized = _normalize_pattern(token)
        if normalized and normalized not in filtered:
            filtered.append(normalized)
    return filtered


def _classify_constraint_type(line_lower: str) -> str | None:
    if any(keyword in line_lower for keyword in _DENY_KEYWORDS):
        return "always_deny"
    if any(keyword in line_lower for keyword in _CHECK_IN_KEYWORDS):
        return "always_check_in"
    if any(keyword in line_lower for keyword in _ALLOW_KEYWORDS):
        return "always_allow"
    return None


def _classify_split_policies(line_lower: str) -> tuple[str | None, str | None] | None:
    if "read" not in line_lower and "write" not in line_lower:
        return None

    def _policy(
        allow_keywords: tuple[str, ...],
        deny_keywords: tuple[str, ...],
        checkin_keywords: tuple[str, ...],
    ) -> str | None:
        if any(keyword in line_lower for keyword in deny_keywords):
            return "always_deny"
        if any(keyword in line_lower for keyword in checkin_keywords):
            return "always_check_in"
        if any(keyword in line_lower for keyword in allow_keywords):
            return "always_allow"
        return None

    read_policy = _policy(_READ_ALLOW_KEYWORDS, _READ_DENY_KEYWORDS, _READ_CHECKIN_KEYWORDS)
    write_policy = _policy(_WRITE_ALLOW_KEYWORDS, _WRITE_DENY_KEYWORDS, _WRITE_CHECKIN_KEYWORDS)
    if read_policy is None and write_policy is None:
        return None
    return read_policy, write_policy


def _looks_like_guideline(line_lower: str) -> bool:
    return any(keyword in line_lower for keyword in _GUIDELINE_KEYWORDS)


def parse_constraints_from_text(text: str, source: str) -> ParseResult:
    """Parse markdown rules into hard constraints and behavioral guidelines."""
    constraints: list[HardConstraint] = []
    guidelines: list[str] = []
    unresolved: list[str] = []
    in_code_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if line.startswith("#"):
            continue

        line_lower = line.lower()
        split_policies = _classify_split_policies(line_lower)
        if split_policies is not None:
            paths = _extract_path_tokens(line)
            if not paths:
                guidelines.append(line)
                unresolved.append(line)
                continue
            read_policy, write_policy = split_policies
            for path in paths:
                constraints.append(
                    HardConstraint(
                        path_pattern=path,
                        source=source,
                        overridable=False,
                        read_policy=read_policy,
                        write_policy=write_policy,
                    )
                )
            continue

        constraint_type = _classify_constraint_type(line_lower)
        if constraint_type is None:
            if _looks_like_guideline(line_lower):
                guidelines.append(line)
            continue

        paths = _extract_path_tokens(line)
        if not paths:
            guidelines.append(line)
            unresolved.append(line)
            continue

        for path in paths:
            constraints.append(
                HardConstraint.for_both(
                    path_pattern=path,
                    constraint_type=constraint_type,
                    source=source,
                    overridable=False,
                )
            )

    unique: dict[tuple[str, str, str, str], HardConstraint] = {}
    for item in constraints:
        key = (item.path_pattern, str(item.read_policy), str(item.write_policy), item.source)
        unique[key] = item
    return ParseResult(
        constraints=list(unique.values()),
        behavioral_guidelines=list(dict.fromkeys(guidelines)),
        unresolved_lines=unresolved,
    )


def compile_manual_constraint_rule(rule: str, source: str) -> ParseResult:
    """Compile a single natural-language rule into the safest deterministic constraint form."""
    normalized = " ".join(rule.split()).strip()
    if not normalized:
        return ParseResult(constraints=[], behavioral_guidelines=[], unresolved_lines=[])

    line_lower = normalized.lower()
    paths = _extract_path_tokens(normalized)
    if (
        paths
        and any(keyword in line_lower for keyword in _DENY_KEYWORDS)
        and not any(keyword in line_lower for keyword in _READ_DENY_KEYWORDS)
    ):
        constraints = [
            HardConstraint(
                path_pattern=path,
                source=source,
                overridable=False,
                read_policy="always_allow",
                write_policy="always_deny",
            )
            for path in paths
        ]
        return ParseResult(
            constraints=constraints,
            behavioral_guidelines=[],
            unresolved_lines=[],
        )
    return parse_constraints_from_text(normalized, source=source)


def parse_constraints_file(path: Path) -> ParseResult:
    source = path.name
    try:
        content = path.read_text()
    except Exception as exc:
        raise RuntimeError(f"Failed to read constraints file: {path}: {exc}") from exc
    return parse_constraints_from_text(content, source=source)
