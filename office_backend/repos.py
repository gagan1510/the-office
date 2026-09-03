"""Repository-relative ownership claim validation and overlap checks."""

from pathlib import Path


def normalized_path_claims(values: object) -> list[tuple[str, str]]:
    if values in (None, []):
        return []
    if not isinstance(values, list) or len(values) > 500:
        raise ValueError("pathClaims must be a list of repository/path objects.")
    claims = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Each path claim must be an object.")
        repository = str(item.get("repository", ".")).strip() or "."
        path = str(item.get("path", "")).strip().replace("\\", "/").strip("/")
        if repository.startswith("/") or ".." in Path(repository).parts or not path or ".." in Path(path).parts:
            raise ValueError("Path claims must use safe repository-relative paths.")
        claims.append((repository, path))
    return sorted(set(claims))


def path_claims_overlap(left: tuple[str, str], right: tuple[str, str]) -> bool:
    if left[0] != right[0]:
        return False
    a, b = left[1].rstrip("/"), right[1].rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def scope_path_claims(spec: dict, claims: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Turn plan-relative repository keys into workspace-specific lock keys."""
    if not claims:
        return []
    raw_root = spec.get("path") if spec.get("mode") == "local" else spec.get("destination")
    if not raw_root:
        raise ValueError("Path claims require a configured local workspace.")
    root = Path(str(raw_root)).expanduser().resolve()
    scoped = []
    for repository, path in claims:
        target = root if repository == "." else (root / repository).resolve()
        if target != root and root not in target.parents:
            raise ValueError("A path claim points outside the configured workspace.")
        scoped.append((str(target), path))
    return sorted(set(scoped))
