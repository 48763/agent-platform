import pytest
from core.sandbox import Sandbox


def test_allow_path_in_allowed_dir(tmp_path):
    sb = Sandbox({"allowed_dirs": [str(tmp_path)], "writable": True})
    sb.check_path(str(tmp_path / "file.txt"), write=False)  # should not raise


def test_deny_path_outside_allowed_dir(tmp_path):
    sb = Sandbox({"allowed_dirs": [str(tmp_path / "safe")]})
    with pytest.raises(PermissionError, match="不在允許目錄"):
        sb.check_path("/etc/passwd", write=False)


def test_deny_pattern(tmp_path):
    sb = Sandbox({
        "allowed_dirs": [str(tmp_path)],
        "denied_patterns": ["*.env", "*.pem"],
    })
    with pytest.raises(PermissionError, match="黑名單"):
        sb.check_path(str(tmp_path / ".env"), write=False)


def test_readonly_sandbox(tmp_path):
    sb = Sandbox({
        "allowed_dirs": [str(tmp_path)],
        "writable": False,
    })
    sb.check_path(str(tmp_path / "file.txt"), write=False)  # read OK
    with pytest.raises(PermissionError, match="唯讀"):
        sb.check_path(str(tmp_path / "file.txt"), write=True)


def test_command_whitelist():
    sb = Sandbox({
        "allowed_dirs": [],
        "allowed_commands": ["git diff", "git log"],
    })
    sb.check_command("git diff HEAD")  # should not raise
    with pytest.raises(PermissionError, match="禁止執行"):
        sb.check_command("rm -rf /")


def test_empty_sandbox_allows_nothing():
    sb = Sandbox({"allowed_dirs": []})
    with pytest.raises(PermissionError):
        sb.check_path("/any/path", write=False)
    with pytest.raises(PermissionError):
        sb.check_command("any command")
