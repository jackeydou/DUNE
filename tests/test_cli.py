from typer.testing import CliRunner

from swarmeval import __version__
from swarmeval.cli.main import app


def test_version_flag_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"swarm {__version__}"
