"""Case bundles: a case directory as a tar archive, stored by content hash.

The control plane validates a bundle with the same loader the worker uses, then stores it as
`cases/sha256/<hex>.tar`; each run names that hash, so it always runs the exact case it was
submitted with (docs/services/orchestrator.md#case-bundles).
"""

import hashlib
import io
import os
import tarfile
import tempfile
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from swarmeval.core import CaseError


def bundle_key(sha256: str) -> str:
    return f"cases/sha256/{sha256}.tar"


def bundle_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


MAX_BUNDLE_BYTES = 64 << 20
"""The largest bundle the Control API takes, and the largest an edit may make one."""


@dataclass(frozen=True)
class BundleFile:
    path: str
    """Relative to the case directory, with `/` separators."""
    content: bytes
    mode: int
    """Unix permission bits."""
    link_target: str
    """For a symbolic link, where it points; `content` is empty. Empty for a regular file."""


def _members(case_dir: Path) -> Iterator[Path]:
    """What a bundle holds: regular files and links to them, sorted, without `__pycache__`."""
    for path in sorted(case_dir.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def pack(case_dir: Path) -> bytes:
    """The directory's files, relative to it, in a stable order. Packing what `unpack`
    extracted gives the bundle's canonical bytes: the same for every archive of the same files,
    modes, and links, whichever tool wrote it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in _members(case_dir):
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


def files_of(data: bytes) -> list[BundleFile]:
    """A bundle's files and links, by path, as `pack` would archive them once unpacked."""
    with tempfile.TemporaryDirectory(prefix="swarmeval-case-") as scratch:
        case_dir = unpack(data, Path(scratch))
        return [
            BundleFile(
                path=path.relative_to(case_dir).as_posix(),
                content=b"" if path.is_symlink() else path.read_bytes(),
                mode=path.lstat().st_mode & 0o777,
                link_target=os.readlink(path) if path.is_symlink() else "",
            )
            for path in _members(case_dir)
        ]


def _target(case_dir: Path, path: str) -> Path:
    """Where a change to `path` lands. Refuses a path that is not a plain relative one, and one
    that passes through a link or a file, so no change is written outside the directory."""
    pure = PurePosixPath(path)
    if (
        not path
        or "\0" in path
        or pure.is_absolute()
        or pure.as_posix() != path
        or ".." in pure.parts
        or "__pycache__" in pure.parts
    ):
        raise CaseError(
            f"file path {path!r} is not valid. Give a path relative to the case directory, "
            "with `/` separators and no `.`, `..`, or `__pycache__` parts."
        )
    parent = case_dir
    for part in pure.parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise CaseError(
                f"file path {path!r} passes through `{parent.relative_to(case_dir).as_posix()}`, "
                "which is a file or a link, not a directory. Delete it first, or pick "
                "another path."
            )
    return case_dir / pure


def apply_changes(
    base: bytes | None, writes: Mapping[str, bytes], deletes: Collection[str]
) -> tuple[bytes, bool]:
    """`base` (nothing, when `None`) with `deletes` removed and then `writes` written, packed
    again, and whether any file differs from `base`. A written file keeps its mode; a new one
    gets 0644; a link written to becomes a regular file. Raises `CaseError` for a path that is
    not valid, is both written and deleted, or is deleted without being there."""
    both = sorted(set(writes) & set(deletes))
    if both:
        raise CaseError(
            f"{', '.join(f'`{p}`' for p in both)}: written and deleted in one change. "
            "Send each path once."
        )
    with tempfile.TemporaryDirectory(prefix="swarmeval-case-") as scratch:
        case_dir = Path(scratch)
        if base is not None:
            unpack(base, case_dir)
        before = pack(case_dir)
        for path in deletes:
            target = _target(case_dir, path)
            if not target.is_symlink() and not target.is_file():
                raise CaseError(
                    f"cannot delete `{path}`: the revision has no such file. Load the case's "
                    "newest revision and try again."
                )
            target.unlink()
        for path, content in writes.items():
            target = _target(case_dir, path)
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                raise CaseError(
                    f"cannot write `{path}`: it is a directory in this revision. Delete the "
                    "files under it first, or pick another path."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            new = not target.exists()
            target.write_bytes(content)
            if new:
                target.chmod(0o644)
        after = pack(case_dir)
    return after, after != before
