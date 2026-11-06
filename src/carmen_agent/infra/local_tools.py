from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from carmen_agent.infra.runtime import ToolRuntime
from carmen_agent.infra.tools import Capability, Tool, ToolPolicy, ToolScope
from carmen_agent.platforms import parse_command


@dataclass(frozen=True)
class ExecuteLocalActionAdapter(Tool):
    name = "execute_local_action_adapter"
    description = "Execute one explicitly selected task through a user-configured local adapter."
    policy = ToolPolicy(
        scopes=(ToolScope.USER,),
        read_only=False,
        capabilities=frozenset({Capability.LOCAL_EXECUTION, Capability.EXTERNAL_WRITE}),
        required_permissions=frozenset({"actions.execute"}),
        approval="explicit",
    )

    command: str
    payload_json: str
    working_directory: str

    def run(self, runtime: ToolRuntime) -> dict[str, object]:
        command = parse_command(self.command)
        if not command:
            raise ValueError("No action adapter command is configured.")
        runner = runtime.services.get("subprocess_run", subprocess.run)
        result = runner(
            command,
            cwd=Path(self.working_directory),
            input=self.payload_json,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
