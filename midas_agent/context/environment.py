"""Environment context — structured execution environment info for the agent."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EnvironmentContext:
    """Execution environment context injected into agent messages.

    Serializes to XML matching Codex CLI's <environment_context> format.
    All fields are optional — only set fields are included in the XML.
    """
    cwd: str | None = None
    shell: str | None = None
    balance: int | None = None
    iteration: int | None = None
    available_agents: list[str] = field(default_factory=list)

    def serialize_to_xml(self) -> str:
        lines: list[str] = []
        if self.cwd is not None:
            lines.append(f"  <cwd>{self.cwd}</cwd>")
        if self.shell is not None:
            lines.append(f"  <shell>{self.shell}</shell>")
        if self.balance is not None:
            lines.append(f"  <balance>{self.balance}</balance>")
        if self.iteration is not None:
            lines.append(f"  <iteration>{self.iteration}</iteration>")
        if self.available_agents:
            lines.append("  <available_agents>")
            for agent_line in self.available_agents:
                lines.append(f"    {agent_line}")
            lines.append("  </available_agents>")
        return "<environment_context>\n" + "\n".join(lines) + "\n</environment_context>"
