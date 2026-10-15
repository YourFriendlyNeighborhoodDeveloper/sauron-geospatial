"""Quarantine output written by the original pipeline (reports/hazards.csv and reports/*.txt).

    python -m tools.migrate_legacy_reports [--reports-dir reports] [--move-originals]

What it does, in order:
1. Writes reports/legacy/backup-<stamp>.tar.gz with every legacy file and a
   SHA-256 manifest, then re-reads the archive to verify every hash.
2. Splits hazards.csv by row shape. The original writer kept a 14-column
   header while later rows had 18 columns, so most rows were misaligned.
   Rows are written under the header that matches their shape, de-duplicated
   by id, and marked legacy_status=unvalidated. Rows of any other shape go
   to quarantine.csv with their line number.
3. With --move-originals (and only after step 1 verified), moves the legacy
   files into reports/legacy/original/. Nothing is deleted.

The legacy rows are never imported into the SAURON store: their coordinates
came from the old geocoder and were not validated.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import tarfile
from collections import Counter
from datetime import datetime, timezone

V0_COLUMNS = "id,ts,type,severity,confidence,status,source,lat,lon,road,direction,lanes_blocked,speed_impact,description".split(",")
V1_COLUMNS = ("id,ts,type,severity,confidence,status,source,lat,lon,road,direction,lanes_blocked,"
              "lanes_blocked_confidence,extraction_version,exit,mile_marker,speed_impact,description").split(",")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def legacy_files(folder):
    out = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and (name == "hazards.csv" or name.endswith(".txt")):
            out.append(path)
    return out


def backup(files, folder, stamp):
    os.makedirs(os.path.join(folder, "legacy"), exist_ok=True)
    archive = os.path.join(folder, "legacy", f"backup-{stamp}.tar.gz")
    hashes = {os.path.basename(p): sha256(p) for p in files}
    with tarfile.open(archive, "w:gz") as tar:
        for p in files:
            tar.add(p, arcname=os.path.basename(p))
        data = json.dumps(hashes, indent=1, sort_keys=True).encode()
        info = tarfile.TarInfo("SHA256SUMS.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "SHA256SUMS.json":
                continue
            digest = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
            if digest != hashes[member.name]:
                raise SystemExit(f"backup verification failed for {member.name}")
        if len(tar.getmembers()) != len(files) + 1:
            raise SystemExit("backup verification failed: member count mismatch")
    return archive, hashes


def split_csv(path, out_dir):
    shapes, seen, dupes = Counter(), {"v0": set(), "v1": set()}, Counter()
    writers, handles = {}, []
    for name, cols in (("v0", V0_COLUMNS), ("v1", V1_COLUMNS)):
        f = open(os.path.join(out_dir, f"hazards_legacy_{name}.csv"), "w", newline="")
        handles.append(f)
        w = csv.writer(f)
        w.writerow(cols + ["legacy_status"])
        writers[name] = w
    qf = open(os.path.join(out_dir, "quarantine.csv"), "w", newline="")
    handles.append(qf)
    quarantine = csv.writer(qf)
    quarantine.writerow(["line", "columns", "raw"])
    with open(path, newline="", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader)
        for line_no, row in enumerate(reader, start=2):
            shape = {len(V0_COLUMNS): "v0", len(V1_COLUMNS): "v1"}.get(len(row))
            shapes[shape or f"other_{len(row)}"] += 1
            if not shape:
                quarantine.writerow([line_no, len(row), ",".join(row)])
                continue
            if row[0] in seen[shape]:
                dupes[shape] += 1
                continue
            seen[shape].add(row[0])
            writers[shape].writerow(row + ["unvalidated"])
    for f in handles:
        f.close()
    return {"header_columns": len(header), "rows_by_shape": dict(shapes),
            "unique_rows": {k: len(v) for k, v in seen.items()}, "duplicates_removed": dict(dupes)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--move-originals", action="store_true")
    args = parser.parse_args()
    folder = args.reports_dir
    files = legacy_files(folder)
    if not files:
        print("no legacy files found")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive, hashes = backup(files, folder, stamp)
    out_dir = os.path.join(folder, "legacy")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backup": os.path.basename(archive),
        "backup_sha256": sha256(archive),
        "files_backed_up": len(files),
        "text_reports": sum(1 for p in files if p.endswith(".txt")),
        "notes": [
            "Rows are split by shape; the original header did not match most rows.",
            "The original writer replaced commas in road and description with spaces; this cannot be undone.",
            "Coordinates come from the original geocoder and were not validated.",
            "Nothing here is imported into the SAURON store.",
        ],
    }
    csv_path = os.path.join(folder, "hazards.csv")
    if os.path.exists(csv_path):
        manifest["hazards_csv"] = {"sha256": hashes["hazards.csv"], **split_csv(csv_path, out_dir)}
    if args.move_originals:
        dest = os.path.join(out_dir, "original")
        os.makedirs(dest, exist_ok=True)
        for p in files:
            shutil.move(p, os.path.join(dest, os.path.basename(p)))
        manifest["originals_moved_to"] = "legacy/original"
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
