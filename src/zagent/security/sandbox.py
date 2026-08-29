from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from zagent.domain.errors import ValidationError


@dataclass(frozen=True)
class SandboxPolicy:
    read_paths: tuple[str, ...] = field(default_factory=tuple)
    write_paths: tuple[str, ...] = field(default_factory=tuple)
    network: bool = False

    @classmethod
    def build(
        cls,
        read_paths: Iterable[str] = (),
        write_paths: Iterable[str] = (),
        *,
        network: bool = False,
    ) -> "SandboxPolicy":
        return cls(
            tuple(_normalize_existing(path) for path in read_paths if path),
            tuple(_normalize_existing(path) for path in write_paths if path),
            network,
        )


class SandboxLauncher:
    """Fail-closed OS sandbox command wrapper for extension/MCP child processes."""

    def __init__(self, system: str | None = None) -> None:
        self.system = (system or platform.system()).lower()

    def available(self) -> bool:
        if self.system == "darwin":
            if not Path("/usr/bin/sandbox-exec").is_file():
                return False
            probe = subprocess.run(
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    "(version 1) (allow default)",
                    "/usr/bin/true",
                ],
                capture_output=True,
                check=False,
                timeout=2,
            )
            return probe.returncode == 0
        if self.system == "linux":
            return shutil.which("bwrap") is not None
        return False

    def wrap(
        self, executable: str, args: list[str], policy: SandboxPolicy, *, enabled: bool = True
    ) -> list[str]:
        command = [executable, *args]
        if not enabled:
            return command
        if self.system == "darwin" and self.available():
            return ["/usr/bin/sandbox-exec", "-p", self._macos_profile(executable, policy), *command]
        if self.system == "linux" and (bwrap := shutil.which("bwrap")):
            return self._linux_command(bwrap, executable, args, policy)
        raise ValidationError(
            "OS sandbox is unavailable; refusing to execute third-party code. "
            "Install bubblewrap on Linux or explicitly disable sandbox after reviewing the risk."
        )

    @staticmethod
    def runtime_read_paths(executable: str) -> tuple[str, ...]:
        candidates = {
            str(Path(executable).resolve()),
            str(Path(executable).resolve().parent),
            str(Path(sys.prefix).resolve()),
        }
        for path in ("/System", "/usr/lib", "/usr/share", "/Library", "/private/var/db/dyld"):
            if Path(path).exists():
                candidates.add(path)
        return tuple(sorted(candidates))

    def _macos_profile(self, executable: str, policy: SandboxPolicy) -> str:
        read_paths = set(self.runtime_read_paths(executable))
        read_paths.update(policy.read_paths)
        read_paths.update(policy.write_paths)
        rules = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-info*)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read-metadata)",
            '(allow file-write-data (literal "/dev/null"))',
        ]
        for path in sorted(read_paths):
            rules.append(f'(allow file-read* (subpath "{_escape_profile(path)}"))')
        for path in sorted(policy.write_paths):
            rules.append(f'(allow file-write* (subpath "{_escape_profile(path)}"))')
        if policy.network:
            rules.append("(allow network-outbound)")
        return " ".join(rules)

    @staticmethod
    def _linux_command(
        bwrap: str, executable: str, args: list[str], policy: SandboxPolicy
    ) -> list[str]:
        command = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if policy.network:
            command.append("--share-net")
        system_roots = ["/usr", "/bin", "/lib", "/lib64", "/etc/ssl", "/etc/resolv.conf"]
        read_paths = set(SandboxLauncher.runtime_read_paths(executable))
        read_paths.update(policy.read_paths)
        for path in system_roots:
            if Path(path).exists():
                read_paths.add(path)
        for path in sorted(read_paths):
            command.extend(["--ro-bind", path, path])
        for path in sorted(policy.write_paths):
            command.extend(["--bind", path, path])
        command.extend(["--", executable, *args])
        return command


def _normalize_existing(value: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise ValidationError(f"sandbox path does not exist: {value}")
    return str(path)


def _escape_profile(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValidationError("invalid sandbox path")
    return value.replace("\\", "\\\\").replace('"', '\\"')
