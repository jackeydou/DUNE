# swarmeval

Launch LLM agent swarms in controlled environments and record their trajectories for safety
analysis: collective deception, result hacking, covert communication.

Status: M0 in progress. The pieces are built and run end to end against a scripted model:
the Control API and worker, model-gateway, the Message Bus, canaries and final-state scorers
(`swarmeval/`), and sandboxd's docker driver (`go/`). Left: the `scorer_misbelief` case and its
run on a real open-weight model. How the system fits together is in
[docs/architecture.md](docs/architecture.md); setup and tasks are in
[docs/development.md](docs/development.md).
