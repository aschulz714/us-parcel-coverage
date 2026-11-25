"""Small atomic-write helpers for release-critical local artifacts.

Writers receive a temporary path on the same volume as the destination. The
destination is replaced only after the writer returns successfully, so a crash,
disk-full error, or serialization failure cannot destroy the last good file.
"""
import json
import os
import tempfile
import time


def atomic_replace_writer(path, writer):
    """Run ``writer(temp_path)`` and atomically replace ``path`` on success."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    base = os.path.basename(path)
    fd, temp_path = tempfile.mkstemp(prefix=f".{base}.", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        writer(temp_path)
        if not os.path.exists(temp_path):
            raise RuntimeError(f"atomic writer did not create temporary file: {temp_path}")
        # Windows denies replacement while a reader holds the destination
        # without FILE_SHARE_DELETE. Retry only that transient class of error;
        # all other write failures surface immediately and preserve the target.
        for attempt in range(601):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt == 600:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def atomic_write_json(path, value, **dump_kwargs):
    """Serialize JSON and atomically replace ``path``."""
    options = {"indent": 1, "sort_keys": True}
    options.update(dump_kwargs)

    def _write(temp_path):
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, **options)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    atomic_replace_writer(path, _write)
