"""An extension instance's state while a run is going: its committed and current state, and the
random streams `ctx.rng` draws from."""

import hashlib
import random

from swarmeval.runtime.extensions.api import AnyHandler, StateCell
from swarmeval.runtime.extensions.registry import LoadedExtension
from swarmeval.runtime.records import ExtensionSnapshot, HookName


class Draws(random.Random):
    """`ctx.rng` for one hook call. It takes its seed on the first draw, from the run seed, the
    instance id, and the instance's count of calls that drew so far, then counts itself. A
    resumed or forked run that restores the count gets the same streams from there on."""

    def __init__(self, instance: "Instance") -> None:
        super().__init__(0)
        self._instance = instance
        self._seeded = False

    def _seed_once(self) -> None:
        if not self._seeded:
            self._seeded = True
            super().seed(self._instance.claim_stream())

    def random(self) -> float:
        self._seed_once()
        return super().random()

    def getrandbits(self, k: int, /) -> int:
        self._seed_once()
        return super().getrandbits(k)


class Instance:
    def __init__(
        self, loaded: LoadedExtension, saved: ExtensionSnapshot | None, seed: int, timeout_s: float
    ):
        self.loaded = loaded
        self.id = loaded.instance_id
        if saved is None:
            state, self.rng_uses = loaded.initial_state(), 0
        else:
            state = loaded.extension.state.model_validate(saved.state)
            self.rng_uses = saved.rng_uses
        self.state = StateCell(state)
        self.committed = ExtensionSnapshot(state.model_dump(mode="json"), self.rng_uses)
        self.seed = seed
        self.timeout_s = loaded.extension.hook_timeout_s or timeout_s

    def handlers(self, hook: HookName) -> list[AnyHandler]:
        return self.loaded.registrations.handlers.get(hook, [])

    def claim_stream(self) -> int:
        digest = hashlib.sha256(f"{self.seed}:{self.id}:{self.rng_uses}".encode()).digest()
        self.rng_uses += 1
        return int.from_bytes(digest[:8], "big")

    def snapshot(self) -> ExtensionSnapshot | None:
        """The state to commit, when it differs from the last committed."""
        current = ExtensionSnapshot(self.state.value.model_dump(mode="json"), self.rng_uses)
        if current == self.committed:
            return None
        self.committed = current
        return current
