from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lean-weave")
except PackageNotFoundError:
    __version__ = "0.1.0"

from lean_weave.cli import main as cli_main


def main() -> None:
    cli_main()
