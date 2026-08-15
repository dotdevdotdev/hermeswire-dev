"""
Centralized path management for HermesWire directories and files.

All paths under ~/.hermeswire/ should use these helpers to ensure
consistency and automatic directory creation.
"""

from pathlib import Path


def hermeswire_dir() -> Path:
    """Return ~/.hermeswire/, creating if needed.

    Returns:
        Path to the HermesWire configuration directory.

    Example:
        config_dir = hermeswire_dir()
        # /Users/user/.hermeswire/
    """
    path = Path.home() / ".hermeswire"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """Return path to config.yaml.

    Returns:
        Path to ~/.hermeswire/config.yaml (may not exist).

    Example:
        if config_path().exists():
            config = load_yaml(config_path())
    """
    return hermeswire_dir() / "config.yaml"


def machines_path() -> Path:
    """Return path to machines.json.

    Returns:
        Path to ~/.hermeswire/machines.json (may not exist).
    """
    return hermeswire_dir() / "machines.json"


def logs_dir() -> Path:
    """Return ~/.hermeswire/logs/, creating if needed.

    Returns:
        Path to logs directory.
    """
    path = hermeswire_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voices_dir() -> Path:
    """Return ~/.hermeswire/voices/, creating if needed.

    Returns:
        Path to voice samples directory.
    """
    path = hermeswire_dir() / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    """Return ~/.hermeswire/uploads/, creating if needed.

    Returns:
        Path to uploads directory.
    """
    path = hermeswire_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hooks_dir() -> Path:
    """Return ~/.hermeswire/hooks/, creating if needed.

    Returns:
        Path to hooks directory.
    """
    path = hermeswire_dir() / "hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path
