import io
import tarfile
from pathlib import Path

import pytest
from google.protobuf.struct_pb2 import Struct

from swarmeval.control.bundles import apply_changes, files_of, pack, unpack
from swarmeval.control.service import overrides_of
from swarmeval.core import CaseError


def test_a_bundle_round_trips_and_is_byte_stable(tmp_path: Path) -> None:
    case = tmp_path / "case"
    (case / "prompts").mkdir(parents=True)
    (case / "case.yaml").write_text("id: x\n")
    (case / "prompts" / "a.md").write_text("hi")

    first, second = pack(case), pack(case)
    out = unpack(first, tmp_path / "out")

    assert first == second
    assert (out / "prompts" / "a.md").read_text() == "hi"


def tar_with(name: str, *, symlink_to: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for entry, data in (("case.yaml", b"id: x\n"), (name, b"x")):
            info = tarfile.TarInfo(entry)
            if entry == name and symlink_to is not None:
                info.type, info.linkname = tarfile.SYMTYPE, symlink_to
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "bundle",
    [tar_with("../escape.txt"), tar_with("link", symlink_to="/etc/passwd")],
    ids=["dotdot", "symlink"],
)
def test_a_bundle_cannot_write_outside_its_directory(tmp_path: Path, bundle: bytes) -> None:
    with pytest.raises(CaseError, match="not a readable tar archive"):
        unpack(bundle, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_an_absolute_member_lands_inside_the_directory(tmp_path: Path) -> None:
    out = unpack(tar_with("/etc/evil"), tmp_path / "out")

    assert (out / "etc" / "evil").read_bytes() == b"x"


def test_a_bundle_needs_case_yaml_at_its_root(tmp_path: Path) -> None:
    case = tmp_path / "case" / "inner"
    case.mkdir(parents=True)
    (case / "case.yaml").write_text("id: x\n")

    with pytest.raises(CaseError, match=r"no `case\.yaml` at its root"):
        unpack(pack(tmp_path / "case"), tmp_path / "out")


def test_override_numbers_keep_their_integer_type() -> None:
    struct = Struct()
    struct.update({"turns": [3, 4], "temperature": [0.5], "model": ["m1"], "flag": [True]})

    assert overrides_of(struct) == {
        "turns": [3, 4],
        "temperature": [0.5],
        "model": ["m1"],
        "flag": [True],
    }
    assert type(overrides_of(struct)["turns"][0]) is int


def test_an_override_must_be_a_list_of_scalars() -> None:
    struct = Struct()
    struct.update({"model": "m1"})

    with pytest.raises(CaseError, match="must be a list"):
        overrides_of(struct)


def test_an_override_value_may_be_a_list_of_scalars() -> None:
    struct = Struct()
    struct.update({"paraphrased": [[], ["dm", 2]]})

    assert overrides_of(struct) == {"paraphrased": [(), ("dm", 2)]}


def test_an_override_value_may_not_nest_further() -> None:
    struct = Struct()
    struct.update({"paraphrased": [[["dm"]]]})

    with pytest.raises(CaseError, match="values must be scalars or lists of scalars"):
        overrides_of(struct)


def linked_case(root: Path) -> bytes:
    """A bundle with an executable file, a link to a file, and a link to a directory's file."""
    (root / "data").mkdir(parents=True)
    (root / "case.yaml").write_text("id: x\n")
    (root / "data" / "a.txt").write_text("a")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    (root / "alias.txt").symlink_to("data/a.txt")
    return pack(root)


def test_files_of_lists_files_and_links_as_packed(tmp_path: Path) -> None:
    files = {f.path: f for f in files_of(linked_case(tmp_path / "case"))}

    assert sorted(files) == ["alias.txt", "case.yaml", "data/a.txt", "run.sh"]
    assert (files["alias.txt"].link_target, files["alias.txt"].content) == ("data/a.txt", b"")
    assert (files["run.sh"].mode, files["data/a.txt"].mode) == (0o755, 0o644)
    assert files["data/a.txt"].content == b"a"


def test_changes_keep_modes_replace_links_and_report_whether_anything_changed(
    tmp_path: Path,
) -> None:
    base = linked_case(tmp_path / "case")

    same, changed = apply_changes(base, {"data/a.txt": b"a"}, [])
    edited, edited_changed = apply_changes(
        base,
        {"run.sh": b"#!/bin/sh\necho hi\n", "alias.txt": b"own", "new/b.txt": b"b"},
        ["data/a.txt"],
    )

    assert (same, changed) == (base, False)
    assert edited_changed
    files = {f.path: f for f in files_of(edited)}
    assert sorted(files) == ["alias.txt", "case.yaml", "new/b.txt", "run.sh"]
    assert (files["run.sh"].mode, files["new/b.txt"].mode) == (0o755, 0o644)
    assert (files["alias.txt"].link_target, files["alias.txt"].content) == ("", b"own")
    assert unpack(edited, tmp_path / "out").joinpath("run.sh").read_text().endswith("echo hi\n")


def test_changes_start_from_nothing_without_a_base() -> None:
    made, changed = apply_changes(None, {"case.yaml": b"id: x\n", "p/a.md": b"hi"}, [])

    assert changed
    assert [f.path for f in files_of(made)] == ["case.yaml", "p/a.md"]


@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("", "not valid"),
        ("/abs.txt", "not valid"),
        ("../up.txt", "not valid"),
        ("a/../b.txt", "not valid"),
        ("./a.txt", "not valid"),
        ("a//b.txt", "not valid"),
        ("__pycache__/x.pyc", "not valid"),
        ("case.yaml/inner", "passes through `case.yaml`"),
        ("alias.txt/inner", "passes through `alias.txt`"),
        ("data", "is a directory"),
    ],
)
def test_a_change_outside_the_case_directory_is_refused(
    tmp_path: Path, path: str, says: str
) -> None:
    base = linked_case(tmp_path / "case")

    with pytest.raises(CaseError, match=says):
        apply_changes(base, {path: b"x"}, [])
    with pytest.raises(CaseError):
        apply_changes(base, {}, [path])


def test_a_path_written_and_deleted_at_once_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="written and deleted"):
        apply_changes(linked_case(tmp_path / "case"), {"data/a.txt": b"x"}, ["data/a.txt"])
