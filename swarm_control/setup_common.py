"""Fixed setup errors and private durable receipts, without service dependencies."""

import json
import os
from pathlib import Path


class SetupError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message="Review the setup action and its fields.", code="invalid_setup"):
    if not condition:
        raise SetupError(code, message)


def private_json(path, value):
    raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def project_path(value):
    require(isinstance(value, str) and 0 < len(value) <= 4096
            and not any(ord(char) < 32 for char in value), "Select a local repository folder.")
    path = Path(value).expanduser().resolve()
    return path / "swarm.toml" if path.is_dir() else path
