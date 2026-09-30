"""Cases: the `case.yaml` / `env.yaml` models, the loader, and variant expansion.

The format is documented in docs/case-format.md.
"""

from swarmeval.core.loader import (
    AgentPrompts,
    CaseError,
    LoadedCase,
    SandboxPlan,
    Variant,
    load_case,
)
from swarmeval.core.models import CaseFile, EnvFile
from swarmeval.core.plan import run_spec

__all__ = [
    "AgentPrompts",
    "CaseError",
    "CaseFile",
    "EnvFile",
    "LoadedCase",
    "SandboxPlan",
    "Variant",
    "load_case",
    "run_spec",
]
