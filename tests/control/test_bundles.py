import io
import tarfile
from pathlib import Path

import pytest
from google.protobuf.struct_pb2 import Struct

from swarmeval.control.bundles import pack, unpack
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
