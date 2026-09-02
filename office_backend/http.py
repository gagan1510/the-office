"""Safe static-resource routing helpers for the HTTP layer."""

from pathlib import Path


def resolve_ui_asset(root: Path, request_path: str) -> Path | None:
    if not request_path.startswith("/ui/"):
        return None
    ui_root = (root / "ui").resolve()
    target = (ui_root / Path(request_path.removeprefix("/ui/"))).resolve()
    if ui_root not in target.parents or not target.is_file() or target.suffix != ".js":
        return None
    return target
