# swarmeval

Launch LLM agent swarms in controlled environments and record their trajectories for safety
analysis: collective deception, result hacking, covert communication.

Status: M0 in progress. The pieces are built and run end to end against a scripted model:
the Control API and worker, model-gateway, the Message Bus, canaries and final-state scorers
(`swarmeval/`), sandboxd's docker driver (`go/`), and the M0 case `cases/scorer_misbelief`. Left:
running that case on a real open-weight model, which is M0's gate. How the system fits together is in
[docs/architecture.md](docs/architecture.md); setup and tasks are in
[docs/development.md](docs/development.md).
