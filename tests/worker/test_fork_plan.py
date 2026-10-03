"""What a fork can restore of its source's files, and what makes it `fs_partial`."""

from swarmeval.runtime.records import FsChange
from swarmeval.worker.fork import plan


def change(path: str, **fields: object) -> FsChange:
    return FsChange.model_validate(
        {
            "path": path,
            "op": "create",
            "uid": 0,
            "before_sha256": None,
            "after_sha256": "ab",
            **fields,
        }
    )


def test_each_paths_last_change_decides_how_it_comes_back() -> None:
    changes = {
        ("box", "/w/note.txt"): change("/w/note.txt", content_stored=True, mode=0o100600),
        ("box", "/w/gone.txt"): change("/w/gone.txt", op="delete", after_sha256=None),
        ("box", "/w/dir"): change("/w/dir", kind="dir", mode=0o40700),
        ("box", "/w/big.bin"): change("/w/big.bin", size=5 << 20),
        ("box", "/w/link"): change("/w/link", kind="symlink"),
        ("other", "/w/x"): change("/w/x", content_stored=True),
    }

    p = plan(changes, "box")

    assert p.remove == ["/w/gone.txt"]
    assert [(d.path, d.mode) for d in p.dirs] == [("/w/dir", 0o700)]
    assert [path for path, _ in p.files] == ["/w/note.txt"]
    assert p.lost == [
        "/w/big.bin: content stored by hash only (5242880 bytes)",
        "/w/link: a symlink, which is not restored",
    ]
