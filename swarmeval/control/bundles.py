"""Case bundles: a case directory as a tar archive, stored by content hash.

The control plane validates a bundle with the same loader the worker uses, then stores it as
`cases/sha256/<hex>.tar`; each run names that hash, so it always runs the exact case it was
submitted with (docs/services/orchestrator.md#case-bundles).
"""

import hashlib
import io
import tarfile
from pathlib import Path

from swarmeval.core import CaseError


def bundle_key(sha256: str) -> str:
    return f"cases/sha256/{sha256}.tar"


def bundle_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack(case_dir: Path) -> bytes:
    """The directory's files, relative to it, in a stable order."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(case_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                info = tar.gettarinfo(path, arcname=path.relative_to(case_dir).as_posix())
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                with path.open("rb") as src:
                    tar.addfile(info, src)
    return buffer.getvalue()


def unpack(data: bytes, into: Path) -> Path:
    """Extracts a bundle with tarfile's `data` filter, which refuses absolute paths, `..`, links
    that leave the directory, and device files. Returns the case directory."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            tar.extractall(into, filter="data")
    except (tarfile.TarError, OSError) as err:
        raise CaseError(f"the case bundle is not a readable tar archive: {err}") from err
    if not (into / "case.yaml").is_file():
        raise CaseError(
            "the case bundle has no `case.yaml` at its root. Archive the case directory's "
            "contents, not the directory itself."
        )
    return into
