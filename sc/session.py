from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClaudeSession:
    system_prompt: str
    messages: list[dict[str, str]] = field(default_factory=list)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
