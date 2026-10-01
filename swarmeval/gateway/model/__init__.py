"""model-gateway (docs/services/model-gateway.md) and the worker's client for it.

Submodules are imported directly: `client` is the worker's side and must not pull in the
`openai` SDK, which only `upstream` imports.
"""
