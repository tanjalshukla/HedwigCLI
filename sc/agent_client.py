from __future__ import annotations

# thin wrapper around AnthropicBedrock that enforces the structured JSON protocol.
# the model is untrusted — it proposes, the CLI validates and enforces.
# two main methods: declare_intent (planning) and generate_updates (implementation).
# both retry once on invalid JSON and validate check-in quality before accepting.

import json
import re
import time
from typing import Any

from anthropic import AnthropicBedrock

from .checkin_quality import build_checkin_repair_prompt, assess_checkin_quality
from .schema import (
    AutonomyRationale,
    CheckInMessage,
    IntentDeclaration,
    LogicNoteCompilation,
    ReadRequest,
    RuleCompilation,
)
from .session import ClaudeSession

RUN_SYSTEM_PROMPT = "MODE: CODE"

ASK_SYSTEM_PROMPT = """
MODE: ASK
You are a helpful software engineering assistant.
Answer questions clearly and concisely.
If file context is provided, use it.
Do not propose code changes unless asked.
Do not output JSON or patches.
""".strip()

# schemas are injected into the user prompt so the model knows the expected format
DECLARE_SCHEMA = {
    "task_summary": "string",
    "planned_files": ["string"],
    "planned_actions": ["edit_code", "add_tests", "run_tests"],
    "planned_commands": ["pytest -q"],
    "workflow_phase": "research|planning|implementation|review|null",
    "notes": "string|null",
    "expected_change_types": [
        "general_change",
        "documentation",
        "test_generation",
        "config_change",
        "api_change",
        "data_model_change",
        "dependency_update",
        "error_handling",
    ],
    "requirements_covered": ["string"],
    "potential_deviations": ["string"],
}

READ_REQUEST_SCHEMA = {
    "type": "read_request",
    "files": ["string"],
    "reason": "string|null",
}

CHECKIN_SCHEMA = {
    "type": "check_in",
    "reason": "string",
    "check_in_type": "plan_review|decision_point|progress_update|deviation_notice|phase_transition|uncertainty",
    "content": "string",
    "recommendation": "string|null",
    "options": ["string"],
    "assumptions": ["string"],
    "confidence": "number|null (0.0-1.0)",
}

AUTONOMY_FEEDBACK_SCHEMA = {
    "prefer_fewer_checkins": "boolean",
    "allowed_checkin_topics": ["api", "signature", "schema", "security", "architecture", "config", "test", "deployment"],
    "skip_low_risk_plan_checkpoint": "boolean",
    "scoped_paths": ["demo/checkin/*"],
}

RULE_COMPILATION_SCHEMA = {
    "constraints": [
        {
            "path_pattern": "string (repo-relative path or glob)",
            "read_policy": "always_allow|always_check_in|always_deny",
            "write_policy": "always_allow|always_check_in|always_deny",
            "reason": "string|null",
        }
    ],
    "behavioral_guidelines": ["string"],
    "unresolved": ["string"],
}

LOGIC_NOTE_SCHEMA = {
    "notes": [
        "string (short note capturing what functionality changed, what decision mattered, or what preference shaped the work)"
    ]
}

AUTONOMY_RATIONALE_SCHEMA = {
    "rationale": "string|null (one concise sentence, max ~18 words)"
}


def _is_retryable(exc: Exception) -> bool:
    """True if a Bedrock failure is worth retrying (transient throttle/network)."""
    name = type(exc).__name__
    if name in {"RateLimitError", "APIConnectionError", "APITimeoutError",
                "ThrottlingException", "ServiceUnavailableException",
                "InternalServerError", "ModelStreamErrorException"}:
        return True
    status = getattr(exc, "status_code", None)
    if status in (429, 500, 502, 503, 504):
        return True
    text = str(exc).lower()
    return (
        "throttl" in text
        or "timeout" in text
        or "temporarily" in text
        or "empty response" in text
    )


def _friendly_bedrock_error(exc: Exception) -> Exception:
    """Wrap a fatal Bedrock failure with booth-friendly guidance, preserving the cause."""
    name = type(exc).__name__
    text = str(exc)
    if "ExpiredToken" in text or "InvalidToken" in text or name == "NoCredentialsError":
        return RuntimeError(
            "AWS credentials expired or missing. Run: aws sso login --profile <PROFILE>, then retry."
        )
    if "AccessDenied" in text or name == "AccessDeniedException":
        return RuntimeError(
            "Bedrock access denied for this model/region. Check IAM permissions and inference profile ARN."
        )
    if _is_retryable(exc):
        return RuntimeError(
            "Bedrock unavailable after several retries. Wait a moment and retry the same task."
        )
    return RuntimeError(f"Bedrock error: {text}")


# Matches a JSON `"path": "..."` field as the structured-update stream lands.
# Non-greedy on the value, tolerant of escaped quotes via lookbehind on `\\`.
_PATH_FIELD_RE = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _scan_latest_path(accumulated: str, announced: set[str]) -> str | None:
    """Return the most recent `"path": "..."` value not yet announced, or None.

    Used by the streaming progress UI to label which file is currently being
    written. Only completed (closed-quote) path fields are returned, so we
    never announce a partial filename mid-token.
    """
    latest: str | None = None
    for match in _PATH_FIELD_RE.finditer(accumulated):
        candidate = match.group(1)
        if candidate and candidate not in announced:
            latest = candidate
    return latest


def _scan_completed_files(accumulated: str, start: int) -> tuple[list[tuple[str, str]], int]:
    """Scan the streaming JSON for `{path, content}` objects whose `content`
    string has fully closed. Returns (completed_files, new_start_offset).

    Approach: walk character-by-character from ``start``, track whether we
    are inside a JSON string (with backslash awareness), and look for
    ``"path": "..."`` followed by ``"content": "..."`` where the content
    string's closing quote has landed. When we see a complete pair, emit it
    and advance the cursor past the closing quote so we don't re-emit on
    the next tick.

    This is intentionally tolerant — we don't validate the surrounding
    array structure, we just harvest the (path, content) pairs as they
    finalize. The full json.loads at the end of the stream is still the
    source of truth for the apply step.
    """
    out: list[tuple[str, str]] = []
    i = start
    n = len(accumulated)

    def _read_string(idx: int) -> tuple[str | None, int]:
        # idx points at the opening quote. Return (decoded, idx_after_close)
        # or (None, idx) if the string hasn't fully closed yet.
        if idx >= n or accumulated[idx] != '"':
            return None, idx
        j = idx + 1
        buf: list[str] = []
        while j < n:
            ch = accumulated[j]
            if ch == "\\":
                if j + 1 >= n:
                    return None, idx
                nxt = accumulated[j + 1]
                escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"',
                           "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
                if nxt in escapes:
                    buf.append(escapes[nxt])
                    j += 2
                    continue
                if nxt == "u":
                    if j + 6 > n:
                        return None, idx
                    try:
                        buf.append(chr(int(accumulated[j + 2:j + 6], 16)))
                    except ValueError:
                        buf.append(accumulated[j:j + 6])
                    j += 6
                    continue
                buf.append(nxt)
                j += 2
                continue
            if ch == '"':
                return "".join(buf), j + 1
            buf.append(ch)
            j += 1
        return None, idx

    while i < n:
        # Find next "path" key.
        key_idx = accumulated.find('"path"', i)
        if key_idx == -1:
            break
        # Skip past the key, find the colon, then the opening quote.
        j = key_idx + len('"path"')
        while j < n and accumulated[j] in ' \t\n\r':
            j += 1
        if j >= n or accumulated[j] != ":":
            break
        j += 1
        while j < n and accumulated[j] in ' \t\n\r':
            j += 1
        path_value, after_path = _read_string(j)
        if path_value is None:
            break  # path string not yet closed; come back next tick
        # Now find the "content" key after the path string.
        content_idx = accumulated.find('"content"', after_path)
        if content_idx == -1:
            break
        k = content_idx + len('"content"')
        while k < n and accumulated[k] in ' \t\n\r':
            k += 1
        if k >= n or accumulated[k] != ":":
            break
        k += 1
        while k < n and accumulated[k] in ' \t\n\r':
            k += 1
        content_value, after_content = _read_string(k)
        if content_value is None:
            break  # content string still streaming; pick up next tick
        out.append((path_value, content_value))
        i = after_content
    return out, i


# raised during generate_updates when the model voluntarily pauses for guidance
class ModelCheckInRequired(RuntimeError):
    def __init__(self, message: CheckInMessage) -> None:
        super().__init__(message.reason)
        self.message = message


class ClaudeClient:
    # Per-request timeout (seconds) for Bedrock. Code-generation calls can
    # legitimately take 20-30s on a heavy plan; 60s is well above that and
    # well below the "Hedwig is hung" threshold a booth visitor will tolerate.
    _REQUEST_TIMEOUT_SEC = 60.0

    def __init__(self, model_id: str, region: str) -> None:
        self.model_id = model_id
        self.client = AnthropicBedrock(
            aws_region=region,
            timeout=self._REQUEST_TIMEOUT_SEC,
        )

    # handles both dict-style and object-style response content blocks
    def _response_text(self, response: Any) -> str:
        if hasattr(response, "content"):
            blocks = response.content
            if isinstance(blocks, list):
                chunks: list[str] = []
                for block in blocks:
                    if isinstance(block, dict):
                        chunks.append(block.get("text", "") or "")
                        continue
                    text = getattr(block, "text", None)
                    if text is None and hasattr(block, "get"):
                        text = block.get("text", "")
                    chunks.append(text or "")
                return "".join(chunks)
        if isinstance(response, dict):
            blocks = response.get("content")
            if isinstance(blocks, list):
                return "".join(block.get("text", "") for block in blocks)
        return str(response)

    # Retry budget for throttling / transient connection errors. Booth conditions
    # (shared wifi, Bedrock peak load) make a no-retry client a demo liability.
    _RETRY_DELAYS = (0.5, 1.5, 4.0)

    def _call(
        self,
        session: ClaudeSession,
        max_tokens: int,
        temperature: float,
        on_file: "callable | None" = None,
    ) -> str:
        # Streaming Bedrock call. Tokens arrive as text deltas; we accumulate
        # them and surface live progress through push_thought so the UI never
        # looks frozen during long generations. Falls through to the same
        # retry / empty-response / friendly-error handling as before.
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0,) + self._RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                chunks: list[str] = []
                last_scan_at = time.time()
                announced_paths: set[str] = set()
                file_scan_offset = 0
                emitted_files: set[str] = set()
                announce = None
                try:
                    from .run.ui import announce_above_spinner as _announce
                    announce = _announce
                except Exception:
                    announce = None

                with self.client.messages.stream(
                    model=self.model_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=session.effective_system_prompt(),
                    messages=session.messages,
                ) as stream:
                    for delta in stream.text_stream:
                        if delta:
                            chunks.append(delta)
                        now = time.time()
                        # Throttle path-scans to ~3/s. We only surface a new
                        # filename as it starts streaming — char counts and
                        # generic "streaming" labels are noise to a developer.
                        if now - last_scan_at <= 0.35:
                            continue
                        last_scan_at = now
                        accumulated = "".join(chunks)
                        new_path = _scan_latest_path(accumulated, announced_paths)
                        if new_path is not None:
                            announced_paths.add(new_path)
                            if announce and on_file is None:
                                # Only show the durable "writing" line when
                                # nobody else is rendering the file's diff.
                                try:
                                    announce(f"  [dim]→ writing[/dim] {new_path}")
                                except Exception:
                                    pass
                        # Emit fully-closed (path, content) pairs as soon as
                        # they finalize, so the caller can render diffs
                        # one-by-one instead of after the whole JSON parses.
                        if on_file is not None:
                            completed, file_scan_offset = _scan_completed_files(
                                accumulated, file_scan_offset
                            )
                            for fpath, fcontent in completed:
                                if fpath in emitted_files:
                                    continue
                                emitted_files.add(fpath)
                                try:
                                    on_file(fpath, fcontent)
                                except Exception:
                                    pass
                text = "".join(chunks)
                if not text or not text.strip():
                    raise RuntimeError("Bedrock returned an empty response")
                return text
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt == len(self._RETRY_DELAYS):
                    raise _friendly_bedrock_error(exc) from exc
                # Best-effort booth-friendly status; never fail the call on render error.
                try:
                    from .run.ui import push_thought
                    push_thought(f"Bedrock busy — retrying in {self._RETRY_DELAYS[attempt]:.1f}s")
                except Exception:
                    pass
        # Unreachable: loop either returns or raises. Defensive re-raise.
        assert last_exc is not None
        raise last_exc

    def _call_with_repair(
        self,
        session: ClaudeSession,
        model_cls: Any,
        max_tokens: int,
        temperature: float = 0.0,
    ) -> Any:
        for attempt in range(2):
            raw = self._call(session, max_tokens=max_tokens, temperature=temperature)
            session.add_assistant(raw)
            try:
                return model_cls.model_validate_json(raw)
            except Exception as exc:
                if attempt == 1:
                    raise
                session.add_user(
                    "Return valid JSON only matching the provided schema. "
                    f"Previous error: {exc}"
                )
        raise RuntimeError(f"Failed to obtain valid {model_cls.__name__}.")

    def summarize_autonomy_feedback(self, feedback_text: str) -> dict[str, object] | None:
        text = " ".join(feedback_text.split()).strip()
        if not text:
            return None
        schema_json = json.dumps(AUTONOMY_FEEDBACK_SCHEMA, indent=2)
        session = ClaudeSession(
            "You extract autonomy preferences from developer feedback. Return JSON only."
        )
        session.add_user(
            "Return JSON only.\n"
            "Extract autonomy preferences from developer feedback. Do not include prose.\n"
            "Schema:\n"
            f"{schema_json}\n\n"
            "Rules:\n"
            "- Use only listed check-in topics.\n"
            "- If the feedback expresses frustration with check-ins or a desire for less "
            "interruption, set prefer_fewer_checkins to true.\n"
            "- Only use false / empty arrays when the feedback is clearly unrelated to "
            "autonomy preferences.\n\n"
            "Examples:\n"
            'Feedback: "just do it" -> {"prefer_fewer_checkins":true,...}\n'
            'Feedback: "stop asking me about formatting" -> {"prefer_fewer_checkins":true,...}\n'
            'Feedback: "use tabs instead of spaces" -> {"prefer_fewer_checkins":false,...}\n\n'
            f"Feedback: {text}"
        )
        raw = self._call(session, max_tokens=220, temperature=0.0)
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def compile_rule(
        self,
        rule_text: str,
        *,
        repo_inventory: list[str] | None = None,
        max_tokens: int = 500,
    ) -> RuleCompilation:
        schema_json = json.dumps(RULE_COMPILATION_SCHEMA, indent=2)
        inventory_block = ""
        if repo_inventory:
            inventory_lines = "\n".join(f"- {item}" for item in repo_inventory[:80])
            inventory_block = f"\nKnown repo paths:\n{inventory_lines}\n"

        session = ClaudeSession(
            "You compile developer-written repository rules into either enforced path constraints "
            "or behavioral guidelines. Return JSON only."
        )
        session.add_user(
            "Return JSON only.\n"
            "Classify the developer rule into one or both of these categories:\n"
            "1) constraints: deterministic file/path constraints safe for CLI enforcement.\n"
            "2) behavioral_guidelines: prompt-level guidance that should influence future behavior.\n"
            "If any part is too ambiguous to enforce safely, place that text in unresolved.\n\n"
            "Schema:\n"
            f"{schema_json}\n\n"
            "Rules:\n"
            "- Create a hard constraint only when the path scope and access policy are explicit enough to enforce safely.\n"
            "- Prefer behavioral_guidelines for coding style, planning preferences, test preferences, or vague cautions.\n"
            "- You may output both constraints and behavioral_guidelines if the rule contains both an enforceable path rule and soft guidance.\n"
            "- Use only repo-relative path patterns.\n"
            "- Do not invent paths that are not grounded in the rule text or known repo paths.\n"
            "- If the user refers to a broad area like 'API files' or 'production configs', compile a constraint only if a clear path can be inferred safely; otherwise keep it as guidance or unresolved.\n"
            "- Do not return prose outside the schema.\n"
            f"{inventory_block}\n"
            "Examples:\n"
            'Rule: "Never modify config/prod/." -> {"constraints":[{"path_pattern":"config/prod/*","read_policy":"always_allow","write_policy":"always_deny","reason":"Protect production configs"}],"behavioral_guidelines":[],"unresolved":[]}\n'
            'Rule: "Only check in for API or schema changes." -> {"constraints":[],"behavioral_guidelines":["Only check in for API or schema changes."],"unresolved":[]}\n'
            'Rule: "Be careful with billing logic." -> {"constraints":[],"behavioral_guidelines":["Be careful with billing logic."],"unresolved":["Be careful with billing logic."]}\n\n'
            f"Rule: {rule_text}"
        )
        return self._call_with_repair(session, RuleCompilation, max_tokens=max_tokens)

    def summarize_logic_notes(
        self,
        *,
        task: str,
        intent_summary: str,
        touched_files: list[str],
        change_types: list[str],
        spec_digest: str | None,
        patch_excerpt: str,
        feedback_texts: list[str] | None = None,
        verification_passed: bool | None = None,
        max_tokens: int = 300,
    ) -> LogicNoteCompilation:
        schema_json = json.dumps(LOGIC_NOTE_SCHEMA, indent=2)
        feedback_block = ""
        if feedback_texts:
            feedback_lines = "\n".join(f"- {item}" for item in feedback_texts[:4])
            feedback_block = f"\nDeveloper feedback from this run:\n{feedback_lines}\n"
        verification_text = (
            "Verification passed." if verification_passed is True
            else "Verification reported failures." if verification_passed is False
            else "Verification status unavailable."
        )
        session = ClaudeSession(
            "You summarize completed coding work into short reusable functionality notes. Return JSON only."
        )
        session.add_user(
            "Return JSON only.\n"
            "Write up to 3 short notes that would help the agent recognize semantically similar work later.\n"
            "Each note should capture one or more of:\n"
            "- the functionality that changed\n"
            "- an important architectural or API choice\n"
            "- a developer preference that shaped the work\n"
            "- a validation or verification lesson\n\n"
            "Schema:\n"
            f"{schema_json}\n\n"
            "Rules:\n"
            "- Keep notes concrete and reusable.\n"
            "- Refer to behavior and tradeoffs, not implementation trivia.\n"
            "- Avoid raw file paths unless they are needed to disambiguate the logic.\n"
            "- Do not invent results not supported by the patch or feedback.\n"
            "- Prefer one strong note over several weak ones.\n\n"
            f"Task: {task}\n"
            f"Intent summary: {intent_summary}\n"
            f"Touched files: {', '.join(touched_files) or 'none'}\n"
            f"Observed change types: {', '.join(change_types) or 'none'}\n"
            f"Specification context: {spec_digest or 'none'}\n"
            f"{verification_text}\n"
            f"{feedback_block}\n"
            "Patch excerpt:\n"
            f"{patch_excerpt}"
        )
        return self._call_with_repair(session, LogicNoteCompilation, max_tokens=max_tokens)

    def generate_autonomy_rationale(
        self,
        *,
        stage: str,
        task: str,
        files: list[str],
        policy_summaries: list[str],
        behavioral_guidelines: list[str],
        feedback_snippets: list[str],
        logic_notes: list[str],
        max_tokens: int = 120,
    ) -> AutonomyRationale:
        schema_json = json.dumps(AUTONOMY_RATIONALE_SCHEMA, indent=2)
        session = ClaudeSession(
            "You explain why a deterministic local governance layer allowed work to continue without a check-in. Return JSON only."
        )
        session.add_user(
            "Return JSON only.\n"
            "Explain why Hedwig can continue without a check-in in one short sentence.\n"
            "You are only explaining a decision already made by the CLI. Do not change or justify the policy itself.\n\n"
            "Schema:\n"
            f"{schema_json}\n\n"
            "Rules:\n"
            "- Keep the rationale under 18 words.\n"
            "- Avoid scores, thresholds, and internal jargon.\n"
            "- Prefer referencing developer guidance or prior related work when clearly relevant.\n"
            "- If guidance is irrelevant, summarize the strongest plain-language reason from the policy summaries.\n"
            "- Do not mention implementation details like SQLite, leases, or prompt tokens.\n"
            "- Return null only if no concise rationale can be formed.\n\n"
            f"Stage: {stage}\n"
            f"Task: {task}\n"
            f"Files: {', '.join(files) if files else 'none'}\n"
            "Policy summaries:\n"
            + ("\n".join(f"- {item}" for item in policy_summaries) if policy_summaries else "- none")
            + "\nBehavioral guidance:\n"
            + ("\n".join(f"- {item}" for item in behavioral_guidelines) if behavioral_guidelines else "- none")
            + "\nRelevant prior feedback:\n"
            + ("\n".join(f"- {item}" for item in feedback_snippets) if feedback_snippets else "- none")
            + "\nRelevant prior functionality notes:\n"
            + ("\n".join(f"- {item}" for item in logic_notes) if logic_notes else "- none")
        )
        return self._call_with_repair(session, AutonomyRationale, max_tokens=max_tokens)

    def declare_intent(
        self,
        session: ClaudeSession,
        task: str,
        max_tokens: int,
        temperature: float,
    ) -> IntentDeclaration | ReadRequest | CheckInMessage:
        schema_json = json.dumps(DECLARE_SCHEMA, indent=2)
        read_schema_json = json.dumps(READ_REQUEST_SCHEMA, indent=2)
        checkin_schema_json = json.dumps(CHECKIN_SCHEMA, indent=2)
        declaration_prompt = (
            "Return JSON only.\n"
            "You must return one of: intent declaration, read request, or check-in message.\n"
            "Intent schema:\n"
            f"{schema_json}\n\n"
            "Read request schema:\n"
            f"{read_schema_json}\n\n"
            "Check-in schema:\n"
            f"{checkin_schema_json}\n\n"
            "Before responding, silently verify:\n"
            "1) Each planned file is strictly necessary.\n"
            "2) You cannot solve the task with fewer files.\n"
            "3) Planned actions are minimal and directly required.\n"
            "If any file is optional, remove it.\n\n"
            "Expected change types should reflect the likely implementation categories.\n"
            "If a spec is provided, requirements_covered must map the plan back to concrete spec items.\n"
            "potential_deviations is for concrete divergences from the user's request or the spec — "
            "things you plan to do that the user did not ask for, or asked for but you intend to skip. "
            "Leave it empty unless there is a real divergence. Do NOT use it for design musings, "
            "alternative approaches you considered but rejected, or commentary on the plan itself.\n\n"
            "Use check_in when you must choose between multiple valid approaches,\n"
            "or when design intent is ambiguous.\n\n"
            "Check-in quality requirements:\n"
            "- Focus on architecture-level concerns and expensive-to-reverse choices.\n"
            "- Include options, tradeoffs, and your recommendation.\n"
            "- Keep it specific to this task and current code context.\n\n"
            "When returning check_in, include:\n"
            "- assumptions: key assumptions you are making (empty list if none)\n"
            "- confidence: confidence in your recommendation (0.0-1.0)\n\n"
            "If provided file contents appear truncated or insufficient, return a read_request instead of intent.\n\n"
            "In notes, include a short 1-3 step plan if helpful, otherwise null.\n\n"
            f"Task: {task}"
        )
        session.add_user(declaration_prompt)
        # try twice — first failure gets a repair prompt, second is fatal
        for attempt in range(2):
            raw = self._call(session, max_tokens=max_tokens, temperature=temperature)
            session.add_assistant(raw)
            try:
                return IntentDeclaration.model_validate_json(raw)
            except Exception:
                try:
                    return ReadRequest.model_validate_json(raw)
                except Exception:
                    try:
                        check_in = CheckInMessage.model_validate_json(raw)
                        quality = assess_checkin_quality(check_in)
                        if quality.valid:
                            return check_in
                        if attempt == 1:
                            raise ValueError(
                                f"Invalid check_in quality: {', '.join(quality.issues)}"
                            )
                        session.add_user(build_checkin_repair_prompt(quality))
                        continue
                    except Exception:
                        if attempt == 1:
                            raise
                        session.add_user(
                            "Your response did not match any valid schema. Return exactly one of:\n"
                            "Intent: {\"task_summary\":\"...\",\"planned_files\":[...],\"planned_actions\":[...],\"planned_commands\":[],\"expected_change_types\":[...],\"requirements_covered\":[...],\"potential_deviations\":[]}\n"
                            "Read: {\"files\":[\"path\"]}\n"
                            "Check-in: {\"type\":\"check_in\",\"reason\":\"...\",\"check_in_type\":\"decision_point\",\"content\":\"...\",\"recommendation\":\"...\",\"options\":[],\"assumptions\":[],\"confidence\":0.9}\n"
                            "Do NOT mix fields from different schemas."
                        )
                        continue
        raise RuntimeError("Failed to obtain valid intent declaration.")

    def generate_updates(
        self,
        session: ClaudeSession,
        declaration: IntentDeclaration,
        file_context: dict[str, str],
        max_tokens: int,
        temperature: float,
        repair_hint: str | None = None,
        on_file: "callable | None" = None,
    ) -> dict[str, str]:
        decl_json = declaration.model_dump_json(indent=2)
        context_blocks: list[str] = []
        for path, content in file_context.items():
            context_blocks.append(f"FILE: {path}\n-----\n{content}\n-----")
        context_blob = "\n\n".join(context_blocks)
        patch_prompt = (
            "Return JSON only.\n"
            "Return a JSON object with key 'files' containing a list of objects:\n"
            "{ \"path\": \"...\", \"content\": \"...\" }\n"
            "The content must be a JSON string using \\n for newlines.\n"
            "Include only files that should change, and only from this list:\n"
            f"{json.dumps(declaration.planned_files)}\n\n"
            "Declaration JSON:\n"
            f"{decl_json}\n\n"
            "Current file contents:\n"
            f"{context_blob}"
        )
        if repair_hint:
            patch_prompt = (
                "Previous response was invalid.\n"
                f"Error: {repair_hint}\n"
                "Return valid JSON only. Use \\n in content strings.\n\n"
            ) + patch_prompt
        session.add_user(patch_prompt)
        for attempt in range(2):
            # Only stream-render on the first attempt; on a repair retry the
            # caller has already seen partial diffs from the bad response.
            stream_cb = on_file if attempt == 0 else None
            raw = self._call(
                session,
                max_tokens=max_tokens,
                temperature=temperature,
                on_file=stream_cb,
            )
            session.add_assistant(raw)
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Response must be a JSON object.")
                if payload.get("type") == "check_in":
                    message = CheckInMessage.model_validate(payload)
                    quality = assess_checkin_quality(message)
                    if not quality.valid:
                        if attempt == 1:
                            raise ValueError(
                                f"Invalid check_in quality: {', '.join(quality.issues)}"
                            )
                        session.add_user(build_checkin_repair_prompt(quality))
                        continue
                    raise ModelCheckInRequired(message)
                files = payload.get("files")
                if not isinstance(files, list):
                    raise ValueError("Missing files array.")
                updates: dict[str, str] = {}
                for item in files:
                    if not isinstance(item, dict):
                        raise ValueError("Each file entry must be an object.")
                    path = item.get("path")
                    content = item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str):
                        raise ValueError("path and content must be strings.")
                    updates[path] = content
                return updates
            except ModelCheckInRequired:
                # Legitimate model-initiated check-in — propagate to the caller,
                # never swallow into the JSON-repair retry loop.
                raise
            except Exception as exc:
                if attempt == 1:
                    raise
                session.add_user(
                    f"Return valid JSON only. Error: {exc}. Use \\n in content."
                )
        raise RuntimeError("Failed to obtain valid file updates.")
