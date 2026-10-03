"""Channel interventions in a case: expanding a channel's `interventions:` into the built-in
`swarmeval.bus.*` extensions, and checking every built-in intervention's config against the
case's channels and turn policy (docs/case-format.md#channel-interventions).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue, ValidationError

from swarmeval.core.models import CaseFile, InterventionItem
from swarmeval.gateway.bus.interventions import CONFIGS, DELAY, INJECT
from swarmeval.runtime.extensions import ExtensionUse

_SINGLE_CHANNEL = frozenset({INJECT})
"""Built-ins configured with one `channel`; the rest take `channels`."""


@dataclass(frozen=True)
class Expanded:
    extensions: tuple[ExtensionUse, ...]
    """The case's `extensions:`, then one entry per channel intervention, in channel order."""
    warnings: tuple[str, ...]


def expand(case: CaseFile) -> Expanded:
    """Raises `ValueError` naming the field when a built-in intervention's config is invalid or
    names a channel the case does not declare."""
    entries = [(use, f"extensions[{i}]") for i, use in enumerate(case.extensions)]
    warnings: list[str] = []
    for i, channel in enumerate(case.swarm.channels):
        for j, item in enumerate(channel.interventions):
            field = f"swarm.channels[{i}].interventions[{j}]"
            name, config = _parts(item)
            if name == "log":
                warnings.append(
                    f"`{field}`: `log` does nothing; every message is already recorded as "
                    "`msg.send` and `msg.deliver`. Remove it."
                )
                continue
            key = "channel" if f"swarmeval.bus.{name}" in _SINGLE_CHANNEL else "channels"
            if key in config:
                raise ValueError(
                    f"`{field}` sets `{key}`, but the intervention applies to channel "
                    f"`{channel.id}`, where it is listed. Remove `{key}`."
                )
            target: JsonValue = channel.id if key == "channel" else [channel.id]
            use = ExtensionUse.model_validate(
                {
                    "use": f"swarmeval.bus.{name}",
                    "as": f"{channel.id}.{name}",
                    "config": {**config, key: target},
                }
            )
            entries.append((use, field))
    for use, field in entries:
        _check(case, use, field)
    return Expanded(tuple(use for use, _ in entries), tuple(warnings))


def _parts(item: InterventionItem) -> tuple[str, dict[str, JsonValue]]:
    if isinstance(item, str):
        return item, {}
    ((name, config),) = item.items()
    return name, config


def _check(case: CaseFile, use: ExtensionUse, where: str) -> None:
    model = CONFIGS.get(use.use)
    if model is None:
        return
    if use.use == DELAY and "seconds" in use.config:
        raise ValueError(
            f"`{where}` (`{use.instance_id}`): `seconds` needs the `async` turn policy. Under "
            f"`{case.swarm.turn_policy}` a delay counts the recipient's own turns; use `turns`."
        )
    try:
        config = model.model_validate(use.config)
    except ValidationError as err:
        problems = "; ".join(
            f"`{'.'.join(map(str, e['loc'])) or 'config'}`: {e['msg']}" for e in err.errors()
        )
        raise ValueError(f"`{where}` (`{use.instance_id}`): {problems}") from err
    declared = [c.id for c in case.swarm.channels]
    named: Sequence[str] = (
        [config.model_dump()["channel"]]
        if use.use in _SINGLE_CHANNEL
        else config.model_dump()["channels"]
    )
    unknown = [c for c in named if c not in declared]
    if unknown:
        raise ValueError(
            f"`{where}` (`{use.instance_id}`) names channels {', '.join(unknown)}, which the "
            f"case does not declare. Channels: {', '.join(declared) or 'none'}."
        )
