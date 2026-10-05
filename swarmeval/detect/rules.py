"""Rule sets for scans: the user's keywords and regexes, read from a YAML file
(docs/services/analysis.md#rule-sets).

```yaml
schema_version: 1
rules:
  - id: aws_key
    regex: "AKIA[0-9A-Z]{16}"
    description: an AWS access key id
  - id: mailbox
    keyword: zzINBOX
    ignore_case: true
```
"""

import hashlib
import re
from pathlib import Path
from typing import Literal, Self

import rfc8785
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class RuleSetError(Exception):
    """A rule file that does not read or validate."""


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=100)
    description: str = ""
    keyword: str | None = Field(default=None, min_length=1)
    """Matched literally. Also matched under a single-byte XOR, but only case for case."""
    regex: str | None = Field(default=None, min_length=1)
    """Python `re` syntax, searched in the text of each decoded view."""
    ignore_case: bool = False

    @model_validator(mode="after")
    def _one_pattern(self) -> Self:
        if (self.keyword is None) == (self.regex is None):
            raise ValueError(f"rule `{self.id}` needs exactly one of `keyword` and `regex`")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as err:
                raise ValueError(f"rule `{self.id}`: `regex` does not compile: {err}") from err
        return self

    def pattern(self) -> re.Pattern[str]:
        source = re.escape(self.keyword) if self.keyword is not None else self.regex
        assert source is not None, "checked in _one_pattern"
        return re.compile(source, re.IGNORECASE if self.ignore_case else 0)


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    rules: tuple[Rule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [r.id for r in self.rules]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ValueError(f"rule ids must be unique; repeated: {', '.join(repeated)}")
        return self

    def sha256(self) -> str:
        """Of the validated rules in canonical JSON (RFC 8785), so formatting and comments in
        the file do not change it. A scan is stored, and replaced, per this hash and run."""
        canonical = rfc8785.dumps(self.model_dump(mode="json"))
        return hashlib.sha256(canonical).hexdigest()


def parse_rules(text: str, source: str) -> RuleSet:
    """The rule set in `text`, a rule file's content; `source` names it in errors."""
    try:
        return RuleSet.model_validate(yaml.safe_load(text))
    except yaml.YAMLError as err:
        raise RuleSetError(f"{source} does not read: {err}") from err
    except ValidationError as err:
        raise RuleSetError(
            f"{source} is not a valid rule set:\n{err}\nSee the format in "
            "docs/services/analysis.md#rule-sets."
        ) from err


def load_rules(path: Path) -> RuleSet:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise RuleSetError(f"rule file {path} does not read: {err}") from err
    return parse_rules(text, f"rule file {path}")
