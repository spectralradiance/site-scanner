"""Saved scan progress, so an interrupted scan can be resumed with --resume.

Each completed unit of work (a crawled page, a link check, a browser check, a speed test, a
PageSpeed Insights result) is appended as one JSON line to a file in the state directory the
moment it finishes. On --resume these are loaded and only the missing work is done.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

KINDS = ("pages", "links", "browser", "speed", "psi")


def _plain(obj: Any) -> Any:
    return asdict(obj) if is_dataclass(obj) else obj


class StateStore:
    def __init__(self, directory: Path, fingerprint: dict, resume: bool, log: Callable[[str], None],
                 output: str = ""):
        self.dir = directory
        self.log = log
        self.cache: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
        meta_path = directory / "meta.json"
        old_meta = self._read_meta(meta_path)

        if resume and old_meta:
            self.meta = old_meta
            changed = sorted(k for k in set(fingerprint) | set(old_meta.get("settings", {}))
                             if fingerprint.get(k) != old_meta.get("settings", {}).get(k))
            if changed:
                log("Note: settings differ from the saved scan (" + ", ".join(changed) + "); "
                    "already-saved results keep the old settings.")
            for kind in KINDS:
                self.cache[kind] = self._load(directory / f"{kind}.jsonl")
            counts = ", ".join(f"{len(v)} {k}" for k, v in self.cache.items() if v)
            log(f"Resuming saved scan from {old_meta.get('started', '?')}: {counts or 'nothing saved yet'}")
            self.meta["settings"] = fingerprint
        else:
            if resume:
                log("No saved scan found to resume; starting a new scan.")
            elif old_meta and old_meta.get("status") != "complete":
                log("An unfinished earlier scan of this site was found and will be replaced "
                    "(use --resume to continue it instead).")
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            self.meta = {"started": datetime.now().isoformat(timespec="seconds"), "settings": fingerprint}
        if output:
            self.meta["output"] = output
        self.meta["status"] = "running"
        directory.mkdir(parents=True, exist_ok=True)
        self._write_meta()
        self._files = {k: open(directory / f"{k}.jsonl", "a", encoding="utf-8") for k in KINDS}

    @staticmethod
    def _read_meta(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def saved_output(directory: Path) -> str | None:
        meta = StateStore._read_meta(directory / "meta.json")
        return meta.get("output") if meta else None

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                        out[rec["k"]] = rec["v"]
                    except (ValueError, KeyError, TypeError):
                        continue  # a line cut short by a crash
        except OSError:
            pass
        return out

    def _write_meta(self) -> None:
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "meta.json")

    def get(self, kind: str, key: str) -> Any:
        return self.cache[kind].get(key)

    def put(self, kind: str, key: str, value: Any) -> None:
        value = _plain(value)
        self.cache[kind][key] = value
        fh = self._files[kind]
        fh.write(json.dumps({"k": key, "v": value}, default=_plain) + "\n")
        fh.flush()

    def finish(self, complete: bool) -> None:
        self.meta["status"] = "complete" if complete else "interrupted"
        self.meta["updated"] = datetime.now().isoformat(timespec="seconds")
        self._write_meta()
        for fh in self._files.values():
            fh.close()
