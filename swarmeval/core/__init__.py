"""Cases: the `case.yaml` / `env.yaml` models, the loader, and variant expansion; and suites of
cases.

The format is documented in docs/case-format.md.
"""

from swarmeval.core.errors import CaseError
from swarmeval.core.loader import (
    AgentPrompts,
    LoadedCase,
    Variant,
    choose_models,
    load_case,
)
from swarmeval.core.models import CaseFile, EnvFile
from swarmeval.core.plan import run_spec
from swarmeval.core.suite import (
    LoadedSuite,
    SuiteEntry,
    SuiteError,
    load_suite,
    load_suite_text,
)
from swarmeval.core.topology import FileSeed, SandboxPlan

__all__ = [
    "AgentPrompts",
    "CaseError",
    "CaseFile",
    "EnvFile",
    "FileSeed",
    "LoadedCase",
    "LoadedSuite",
    "SandboxPlan",
    "SuiteEntry",
    "SuiteError",
    "Variant",
    "choose_models",
    "load_case",
    "load_suite",
    "load_suite_text",
    "run_spec",
]
