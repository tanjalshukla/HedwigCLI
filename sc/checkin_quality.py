from __future__ import annotations

from dataclasses import dataclass

from .schema import CheckInMessage


_TRADEOFF_MARKERS = (
    "tradeoff",
    "pros",
    "cons",
    "risk",
    "benefit",
    "cost",
    "option",
    "alternative",
)
_RECOMMEND_MARKERS = ("recommend", "prefer", "suggest", "best option")
_ARCHITECTURE_MARKERS = (
    "architecture",
    "design",
    "interface",
    "contract",
    "schema",
    "dependency",
    "workflow",
    "api",
    "endpoint",
    "route",
    "handler",
    "response",
    "envelope",
)

_OPTIONS_REQUIRED = {
    "decision_point",
    "deviation_notice",
    "uncertainty",
    "plan_review",
}


@dataclass(frozen=True)
class CheckInQualityResult:
    valid: bool
    issues: tuple[str, ...]


def evaluate_checkin_quality(message: CheckInMessage) -> CheckInQualityResult:
    """Validate that model-initiated check-ins are architecturally meaningful."""
    issues: list[str] = []
    reason = message.reason.strip()
    content = message.content.strip()
    combined = f"{reason}\n{content}".lower()
    options = message.options or []

    if len(reason) < 20:
        issues.append("reason is too short")
    if len(content) < 60:
        issues.append("content is too short to explain tradeoffs")
    if message.check_in_type in _OPTIONS_REQUIRED and len(options) < 2:
        issues.append("at least two options are required for this check-in type")
    if message.confidence is None:
        issues.append("missing confidence")
    if message.assumptions is None:
        issues.append("missing assumptions list")
    if not any(marker in combined for marker in _TRADEOFF_MARKERS):
        issues.append("missing explicit tradeoff language")
    if not any(marker in combined for marker in _RECOMMEND_MARKERS):
        issues.append("missing recommendation")
    if not any(marker in combined for marker in _ARCHITECTURE_MARKERS):
        issues.append("missing architectural context")

    return CheckInQualityResult(valid=not issues, issues=tuple(issues))


def build_checkin_repair_prompt(result: CheckInQualityResult) -> str:
    issue_text = "; ".join(result.issues) if result.issues else "unspecified issue"
    return (
        "Your check-in was too shallow for architectural review.\n"
        f"Issues: {issue_text}.\n"
        "Return a check_in JSON only with:\n"
        "- reason: concrete architectural risk or ambiguity\n"
        "- content: options, tradeoffs (pros/cons), and downstream impact\n"
        "- options: at least two concrete approaches when applicable\n"
        "- include your recommendation inside the content field (e.g. 'I recommend ...')\n"
        "- assumptions: list of key assumptions you are making\n"
        "- confidence: number in [0.0, 1.0]\n"
        "Do not return markdown."
    )
