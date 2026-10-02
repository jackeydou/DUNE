"""Offline analysis over exported runs, run as batch jobs: `python -m swarmeval.analysis <job>`
(docs/services/analysis.md)."""

from swarmeval.analysis.report import Rate, Report, Unscored, load_summaries, markdown, report

__all__ = ["Rate", "Report", "Unscored", "load_summaries", "markdown", "report"]
