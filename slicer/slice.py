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


import struct
import xml.etree.ElementTree as ET

NS_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
NS_PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


def strip_3mf(src, dst):
    """Copy a .3mf without its Metadata/, so none of its own printer or process presets are used."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        keep = {i.filename for i in zin.infolist()
                if not i.filename.startswith(("Metadata/", "Auxiliaries/"))}
        for name in zin.namelist():
            if name not in keep:
                continue
            data = zin.read(name)
            if name == "_rels/.rels":
                text = data.decode("utf-8")
                def drop(m):
                    t = re.search(r'Target="/?([^"]*)"', m.group(0))
                    return m.group(0) if (t and t.group(1) in keep) else ""
                text = re.sub(r"<Relationship\b[^>]*/>", drop, text)
                data = text.encode("utf-8")
            zout.writestr(name, data)


def _matrix(text):
    v = [float(x) for x in text.split()] if text else [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    return v if len(v) == 12 else [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


def _apply(m, p):
    x, y, z = p
    return (x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


def _compose(a, b):
    """a first, then b (row-vector convention used by 3MF)."""
    ra = [a[0:3], a[3:6], a[6:9], a[9:12]]
    out = []
    for r in range(4):
        row = ra[r] + ([0.0] if r < 3 else [1.0])
        x, y, z = row[0], row[1], row[2]
        base = [x * b[0] + y * b[3] + z * b[6], x * b[1] + y * b[4] + z * b[7], x * b[2] + y * b[5] + z * b[8]]
        if r == 3:
            base = [base[0] + b[9], base[1] + b[10], base[2] + b[11]]
        out.extend(base)
    return out


def threemf_to_stl(src, dst):
    """Merge every mesh in a .3mf (with transforms) into one binary STL. Returns the triangle count."""
    z = zipfile.ZipFile(src)
    cache = {}

    def model(path):
        path = path.lstrip("/")
        if path not in cache:
            root = ET.fromstring(z.read(path))
            cache[path] = {o.get("id"): o for o in root.iter(NS_CORE + "object")}, root
        return cache[path]

    tris = []

    def emit(path, objid, mat):
        objs, _ = model(path)
        obj = objs[objid]
        mesh = obj.find(NS_CORE + "mesh")
        if mesh is not None:
            verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                     for v in mesh.find(NS_CORE + "vertices")]
            wv = [_apply(mat, v) for v in verts]
            for t in mesh.find(NS_CORE + "triangles"):
                tris.append((wv[int(t.get("v1"))], wv[int(t.get("v2"))], wv[int(t.get("v3"))]))
        comps = obj.find(NS_CORE + "components")
        if comps is not None:
            for c in comps:
                sub = c.get(NS_PROD + "path") or path
                emit(sub, c.get("objectid"), _compose(_matrix(c.get("transform")), mat))

    main = "3D/3dmodel.model"
    _, root = model(main)
    build = root.find(NS_CORE + "build")
    for item in build.iter(NS_CORE + "item"):
        emit(item.get(NS_PROD + "path") or main, item.get("objectid"), _matrix(item.get("transform")))
    if not tris:
        raise RuntimeError("no triangles found in the 3mf")
    with open(dst, "wb") as f:
        f.write(b"converted from 3mf".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            f.write(struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0))
    return len(tris)


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

    model_path = str(Path(MODEL).resolve())
    settings = f"{paths['machine']};{paths['process']}"
    variants = []
    if MODEL.lower().endswith(".3mf"):
        stripped = work / "stripped.3mf"
        try:
            strip_3mf(MODEL, stripped)
            variants.append(("S: 3mf without its own presets", str(stripped.resolve()),
                             ["--allow-newer-file", "--arrange", "1", "--slice", "0"]))
        except Exception as e:
            print("could not strip the 3mf:", e)
        converted = work / "converted.stl"
        try:
            n = threemf_to_stl(MODEL, converted)
            print(f"converted the 3mf to STL: {n} triangles")
            variants.append(("T: mesh converted to STL", str(converted.resolve()), ["--arrange", "1", "--slice", "0"]))
        except Exception as e:
            print("could not convert the 3mf to STL:", e)
    else:
        variants.append(("A: arrange, slice all plates", model_path, ["--arrange", "1", "--slice", "0"]))
        variants.append(("B: no arrange, slice plate 1", model_path, ["--slice", "1"]))

    result = None
    for label, mpath, extra in variants:
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir()
        cmd = (["xvfb-run", "-a", "squashfs-root/AppRun", "--debug", "3"] + extra + [
            "--load-settings", settings,
            "--load-filaments", paths["filament"],
            "--outputdir", str(out.resolve()),
            "--export-3mf", final_name,
            mpath,
        ])
        print(f"\n===== variant {label} =====")
        print("running:", " ".join(cmd), flush=True)
        try:
            r = subprocess.run(cmd, timeout=900)
            print("exit code:", r.returncode)
        except subprocess.TimeoutExpired:
            print("slicer timed out")
        print("files in out/:")
        for f in sorted(out.rglob("*")):
            if f.is_file():
                print("  ", f, f.stat().st_size)
        rj = out / "result.json"
        if rj.is_file():
            print("result.json:", rj.read_text(errors="replace"))
        found = sorted(out.rglob("*.3mf"))
        if found:
            result = found[0]
            break

    if result is None:
        die("the slicer produced no .3mf file in any variant; see the log above")

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
