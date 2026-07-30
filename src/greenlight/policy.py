"""Trusted, versioned review-policy snapshots stored outside judged branches."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from . import config, gitx
from .config import Config, Reviewer, Routing, VerifyTarget
from .util import GreenlightError, repo_id, state_dir

POLICY_VERSION = 1
_POINTER_NAME = "current"


@dataclass(frozen=True)
class PolicySnapshot:
    config: Config
    digest: str
    path: Path
    version: int = POLICY_VERSION


def policy_dir(repo_root: str | Path) -> Path:
    root = gitx.main_repo_root(repo_root)
    return state_dir() / "policies" / repo_id(root)


def _canonical_payload(cfg: Config) -> bytes:
    payload = {"version": POLICY_VERSION, "policy": asdict(cfg)}
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace path atomically with complete, flushed bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as fh:
            temp_path = Path(fh.name)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Keep the rename as the last fallible operation: once it succeeds the
        # complete new value is authoritative, so callers must not report that
        # the old value remains active because a later durability step failed.
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def update(repo_root: str | Path) -> PolicySnapshot:
    """Validate repo config and atomically make its effective policy trusted."""
    root = gitx.main_repo_root(repo_root)
    source = Path(root) / config.CONFIG_NAME
    if not source.is_file():
        raise GreenlightError(
            f"{source} does not exist; create it or run `greenlight init`"
        )
    cfg = config.loads(source.read_text(), str(source))
    data = _canonical_payload(cfg)
    digest = _digest(data)
    directory = policy_dir(root)
    snapshot_path = directory / "snapshots" / f"v{POLICY_VERSION}" / f"{digest}.json"

    try:
        if snapshot_path.exists():
            if snapshot_path.read_bytes() != data:
                raise GreenlightError(
                    f"trusted policy snapshot collision at {snapshot_path}; refusing to replace it"
                )
        else:
            _atomic_write(snapshot_path, data)
        pointer = json.dumps(
            {"digest": digest, "version": POLICY_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode() + b"\n"
        _atomic_write(directory / _POINTER_NAME, pointer)
    except GreenlightError:
        raise
    except OSError as exc:
        raise GreenlightError(
            f"could not update trusted policy atomically; previous policy remains active: {exc}"
        ) from exc
    return PolicySnapshot(cfg, digest, snapshot_path)


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GreenlightError(f"trusted policy {label} must be an object")
    return value


def _decode_config(raw: Any) -> Config:
    data = _require_dict(raw, "payload")
    expected = {field.name for field in fields(Config)}
    if set(data) != expected:
        raise GreenlightError("trusted policy has an incompatible configuration schema")
    try:
        reviewers = [Reviewer(**_require_dict(item, "reviewer")) for item in data["reviewers"]]
        routing = Routing(**_require_dict(data["routing"], "routing"))
        verify_backend = [
            VerifyTarget(**_require_dict(item, "verify target"))
            for item in data["verify_backend"]
        ]
        return Config(
            **{
                **data,
                "reviewers": reviewers,
                "routing": routing,
                "verify_backend": verify_backend,
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GreenlightError(f"trusted policy has invalid configuration: {exc}") from exc


def load(repo_root: str | Path) -> PolicySnapshot:
    """Load authoritative policy only from trusted state, failing closed."""
    directory = policy_dir(repo_root)
    pointer_path = directory / _POINTER_NAME
    work = shlex.quote(gitx.main_repo_root(repo_root))
    recovery = f"run `greenlight policy update --work {work}`"
    if not pointer_path.is_file():
        raise GreenlightError(f"no trusted policy snapshot; {recovery}")
    try:
        pointer = _require_dict(json.loads(pointer_path.read_text()), "pointer")
        version = pointer.get("version")
        digest = pointer.get("digest")
        if version != POLICY_VERSION:
            raise GreenlightError(f"unsupported trusted policy version {version!r}; {recovery}")
        if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise GreenlightError(f"trusted policy pointer has an invalid digest; {recovery}")
        snapshot_path = directory / "snapshots" / f"v{version}" / f"{digest}.json"
        data = snapshot_path.read_bytes()
        if _digest(data) != digest:
            raise GreenlightError(f"trusted policy snapshot digest does not match; {recovery}")
        payload = _require_dict(json.loads(data), "snapshot")
        if payload.get("version") != version or set(payload) != {"version", "policy"}:
            raise GreenlightError(f"trusted policy snapshot has an invalid envelope; {recovery}")
        cfg = _decode_config(payload["policy"])
    except GreenlightError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise GreenlightError(f"trusted policy snapshot is unreadable; {recovery}: {exc}") from exc
    return PolicySnapshot(cfg, digest, snapshot_path, version)
