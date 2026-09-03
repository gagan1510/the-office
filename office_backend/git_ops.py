"""Pure helpers used by diff and selective-publish operations."""

import hashlib
import re


def split_diff_files(patch: str) -> list[dict]:
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", patch)]
    chunks = [patch[start:(starts[index + 1] if index + 1 < len(starts) else len(patch))]
              for index, start in enumerate(starts)]
    files = []
    for chunk in chunks:
        match = re.search(r"(?m)^diff --git a/(.*?) b/(.*?)$", chunk)
        if not match:
            continue
        old_path, new_path = match.groups()
        path = new_path if new_path != "/dev/null" else old_path
        hunk_starts = [item.start() for item in re.finditer(r"(?m)^@@ ", chunk)]
        header_end = hunk_starts[0] if hunk_starts else len(chunk)
        header = chunk[:header_end]
        hunks = []
        for index, start in enumerate(hunk_starts):
            text = chunk[start:(hunk_starts[index + 1] if index + 1 < len(hunk_starts) else len(chunk))]
            hunks.append({"id": hashlib.sha256((path + "\0" + text).encode()).hexdigest()[:20], "patch": text})
        files.append({
            "id": hashlib.sha256((path + "\0" + header).encode()).hexdigest()[:20],
            "path": path, "oldPath": old_path, "newPath": new_path,
            "binary": bool(re.search(r"(?m)^(?:GIT binary patch|Binary files .+ differ)$", chunk)),
            "atomic": bool(re.search(r"(?m)^(?:new file mode|deleted file mode|rename from|rename to) ", header)),
            "header": header, "hunks": hunks, "patch": chunk,
        })
    return files
