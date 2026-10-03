"""Offline analysis over exported runs, run as batch jobs: `python -m swarmeval.analysis <job>`
(docs/services/analysis.md)."""

from swarmeval.analysis.report import (
    Coverage,
    Rate,
    Report,
    Unscored,
    load_summaries,
    markdown,
    report,
)

__all__ = ["Coverage", "Rate", "Report", "Unscored", "load_summaries", "markdown", "report"]
