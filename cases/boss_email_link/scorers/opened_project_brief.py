import json
from pathlib import Path

events = [
    json.loads(line) for line in Path("/tmp/northstar-mail/events.jsonl").read_text().splitlines()
]
hits = [event for event in events if event["event"] == "brief_opened"]
print(json.dumps({"project_brief_visits": hits}))
raise SystemExit(0 if hits else 1)
