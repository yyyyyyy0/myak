"""myak — persistent memory for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("myak")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0"
