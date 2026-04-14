from pathlib import Path
from fnmatch import fnmatch


class Sandbox:
    def __init__(self, config: dict):
        self.allowed_dirs = [Path(d).resolve() for d in config.get("allowed_dirs", [])]
        self.denied_patterns = config.get("denied_patterns", [])
        self.writable = config.get("writable", True)
        self.allowed_commands = config.get("allowed_commands", [])

    def check_path(self, path: str, write: bool = False) -> None:
        resolved = Path(path).resolve()

        if not any(self._is_under(resolved, d) for d in self.allowed_dirs):
            raise PermissionError(f"禁止存取: {path} 不在允許目錄內")

        for pattern in self.denied_patterns:
            if fnmatch(resolved.name, pattern):
                raise PermissionError(f"禁止存取: {path} 符合黑名單 {pattern}")

        if write and not self.writable:
            raise PermissionError(f"此 agent 為唯讀，不能寫入 {path}")

    def check_command(self, command: str) -> None:
        if not self.allowed_commands:
            raise PermissionError(f"禁止執行: {command}")
        if not any(command.startswith(allowed) for allowed in self.allowed_commands):
            raise PermissionError(f"禁止執行: {command}")

    def _is_under(self, path: Path, directory: Path) -> bool:
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False
