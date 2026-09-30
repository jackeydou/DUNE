# swarmeval

Launch LLM agent swarms in controlled environments and record their trajectories for safety
analysis: collective deception, result hacking, covert communication.

Status: M0 in progress. Built so far: case loading and the agent loop (`swarmeval/`), and
sandboxd's docker driver (`go/`). How the system fits together is in
[docs/architecture.md](docs/architecture.md); setup and tasks are in
[docs/development.md](docs/development.md).
