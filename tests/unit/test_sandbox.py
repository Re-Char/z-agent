from __future__ import annotations

import subprocess
import sys

import pytest

from zagent.domain.errors import ValidationError
from zagent.security.sandbox import SandboxLauncher, SandboxPolicy


def test_os_sandbox_executes_or_fails_closed(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    policy = SandboxPolicy.build([str(allowed)], [], network=False)
    launcher = SandboxLauncher()
    if not launcher.available():
        with pytest.raises(ValidationError, match="sandbox is unavailable"):
            launcher.wrap(sys.executable, ["-c", "print('ok')"], policy)
        return
    command = launcher.wrap(sys.executable, ["-c", "print('sandbox-ok')"], policy)
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "sandbox-ok"


def test_sandbox_command_generation_and_validation(tmp_path, monkeypatch):
    read_root = tmp_path / "read"
    write_root = tmp_path / "write"
    read_root.mkdir()
    write_root.mkdir()
    policy = SandboxPolicy.build([str(read_root)], [str(write_root)], network=True)
    launcher = SandboxLauncher(system="windows")
    assert launcher.wrap(sys.executable, ["-V"], policy, enabled=False)[0] == sys.executable
    assert launcher.available() is False
    with pytest.raises(ValidationError, match="sandbox is unavailable"):
        launcher.wrap(sys.executable, [], policy)

    mac_profile = SandboxLauncher(system="darwin")._macos_profile(sys.executable, policy)
    assert "(deny default)" in mac_profile
    assert "(allow network-outbound)" in mac_profile
    assert str(write_root) in mac_profile

    monkeypatch.setattr("zagent.security.sandbox.shutil.which", lambda name: "/usr/bin/bwrap")
    linux = SandboxLauncher(system="linux")
    command = linux.wrap(sys.executable, ["-V"], policy)
    assert command[0] == "/usr/bin/bwrap"
    assert "--share-net" in command
    assert "--ro-bind" in command
    assert "--bind" in command

    with pytest.raises(ValidationError, match="does not exist"):
        SandboxPolicy.build([str(tmp_path / "missing")])
