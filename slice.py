"""Slice a model with the OrcaSlicer command line, using Bambu Lab A1 profiles.

Inputs come from environment variables: MODEL (path under incoming/), MATERIAL, LAYER, INFILL.
Writes models/<name>_<MATERIAL>.gcode.3mf, which the printer can start directly.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

MODEL = os.environ.get("MODEL", "")
MATERIAL = os.environ.get("MATERIAL", "PLA").upper()
LAYER = os.environ.get("LAYER", "0.20")
INFILL = os.environ.get("INFILL", "15")

MACHINE = "Bambu Lab A1 0.4 nozzle"
FILAMENTS = {
    "PLA": ["Bambu PLA Basic @BBL A1", "Generic PLA @BBL A1", "Generic PLA"],
    "PETG": ["Generic PETG @BBL A1", "Bambu PETG Basic @BBL A1", "Generic PETG"],
    "ABS": ["Generic ABS @BBL A1", "Bambu ABS @BBL A1", "Generic ABS"],
    "TPU": ["Generic TPU @BBL A1", "Bambu TPU 95A HF @BBL A1", "Generic TPU"],
}
PROCESSES = {
    "0.08": "0.08mm Extra Fine @BBL A1",
    "0.12": "0.12mm Fine @BBL A1",
    "0.16": "0.16mm Optimal @BBL A1",
    "0.20": "0.20mm Standard @BBL A1",
    "0.24": "0.24mm Draft @BBL A1",
    "0.28": "0.28mm Extra Draft @BBL A1",
}


def die(msg):
    print("ERROR:", msg)
    sys.exit(1)


def check_inputs():
    if not re.fullmatch(r"incoming/[A-Za-z0-9._-]+\.(stl|3mf)", MODEL, re.I) or ".." in MODEL:
        die(f"model path not allowed: {MODEL!r}")
    if not Path(MODEL).is_file():
        die(f"model not found in the repo: {MODEL}")
    if MATERIAL not in FILAMENTS:
        die(f"unknown material {MATERIAL!r}")
    if LAYER not in PROCESSES:
        die(f"unsupported layer height {LAYER!r}; use one of {', '.join(PROCESSES)}")
    if not re.fullmatch(r"\d{1,3}", INFILL) or not 0 <= int(INFILL) <= 100:
        die(f"bad infill {INFILL!r}")


def find_profiles():
    for p in Path("squashfs-root").rglob("BBL"):
        if p.is_dir() and p.parent.name == "profiles":
            return p
    die("could not find the BBL profiles inside OrcaSlicer")


def build_index(root):
    index = {}
    for f in root.rglob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            index.setdefault(data["name"], f)
    return index


def flatten(name, index, depth=0):
    if name not in index:
        die(f"profile not found: {name}")
    if depth > 12:
        die(f"profile inheritance too deep at {name}")
    data = json.loads(index[name].read_text(encoding="utf-8"))
    parent = data.get("inherits")
    merged = flatten(parent, index, depth + 1) if parent else {}
    merged.update(data)
    merged.pop("inherits", None)
    merged["name"] = name
    return merged


def pick(candidates, index, what):
    for c in candidates:
        if c in index:
            return c
    near = sorted(n for n in index if MATERIAL in n.upper() and "A1" in n)[:40]
    die(f"no {what} profile found among {candidates}. Similar names: {near}")


def main():
    check_inputs()
    root = find_profiles()
    index = build_index(root)
    print(f"{len(index)} profiles indexed under {root}")

    work = Path("work")
    out = Path("out")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    work.mkdir()
    out.mkdir()

    machine = flatten(pick([MACHINE], index, "printer"), index)
    machine["from"] = "User"
    machine["printer_settings_id"] = machine["name"]

    process = flatten(pick([PROCESSES[LAYER]], index, "process"), index)
    process["from"] = "User"
    process["print_settings_id"] = process["name"]
    process["compatible_printers"] = []
    process["layer_height"] = LAYER
    process["sparse_infill_density"] = f"{INFILL}%"
    if MATERIAL == "ABS":
        # no enclosure on the A1: a wide brim keeps the corners from lifting
        process["brim_type"] = "outer_only"
        process["brim_width"] = "8"
        process["brim_object_gap"] = "0"

    filament = flatten(pick(FILAMENTS[MATERIAL], index, "filament"), index)
    filament["from"] = "User"
    filament["filament_settings_id"] = [filament["name"]]
    filament["compatible_printers"] = []

    print("machine :", machine["name"])
    print("process :", process["name"], f"(layer {LAYER}, infill {INFILL}%)")
    print("filament:", filament["name"])

    paths = {}
    for kind, data in (("machine", machine), ("process", process), ("filament", filament)):
        p = work / f"{kind}.json"
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        paths[kind] = str(p.resolve())

    base = re.sub(r"\.(stl|3mf)$", "", Path(MODEL).name, flags=re.I)[:40]
    final_name = f"{base}_{MATERIAL}.gcode.3mf"

    cmd = [
        "xvfb-run", "-a", "squashfs-root/AppRun",
        "--arrange", "1",
        "--load-settings", f"{paths['machine']};{paths['process']}",
        "--load-filaments", paths["filament"],
        "--slice", "0",
        "--outputdir", str(out.resolve()),
        "--export-3mf", final_name,
        str(Path(MODEL).resolve()),
    ]
    print("running:", " ".join(cmd))
    try:
        subprocess.run(cmd, timeout=900)
    except subprocess.TimeoutExpired:
        print("slicer timed out")

    print("files in out/:")
    for f in sorted(out.rglob("*")):
        if f.is_file():
            print("  ", f, f.stat().st_size)

    result = out / final_name
    if not result.is_file():
        found = sorted(out.rglob("*.3mf"))
        if not found:
            die("the slicer produced no .3mf file; see the log above")
        result = found[0]

    with zipfile.ZipFile(result) as z:
        names = z.namelist()
    if not any(n.endswith(".gcode") and n.startswith("Metadata/") for n in names):
        print("zip contents:", names)
        die("the result has no Metadata/plate_*.gcode, so it is not a sliced file")

    Path("models").mkdir(exist_ok=True)
    dest = Path("models") / final_name
    shutil.copy(result, dest)
    print(f"OK: {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
