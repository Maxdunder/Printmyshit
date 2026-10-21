"""Slice a model with the OrcaSlicer command line, using Bambu Lab A1 profiles.

Inputs come from environment variables:
  MODEL     path under incoming/ (an .stl)
  MATERIAL  PLA, PETG, ABS or TPU
  LAYER     layer height in mm
  INFILL    infill in percent
  CHANGES   optional AMS color changes:
            "base=1;5.2:2;10.4:3;slots=#ff0000,#00ff00,#0000ff,#ffff00;types=PLA,PETG,,PLA"
            base = AMS slot used first; "height:slot" = from the layer at that height (mm) use that slot;
            types = material loaded in each slot (empty = use MATERIAL)

Writes models/<name>_<MATERIAL>.gcode.3mf (or ..._<MATERIAL>_AMS<n>.gcode.3mf when several
filaments are used), which the printer can start directly.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

MODEL = os.environ.get("MODEL", "")
MATERIAL = os.environ.get("MATERIAL", "PLA").upper()
LAYER = os.environ.get("LAYER", "0.20")
INFILL = os.environ.get("INFILL", "15")
CHANGES = os.environ.get("CHANGES", "").strip()

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

TYPE_ALIAS = {"ASA": "ABS", "PLA+": "PLA", "PLA-CF": "PLA", "PETG-CF": "PETG", "PETG-HF": "PETG",
              "TPU 95A": "TPU", "TPU95A": "TPU"}


def material_for(slot_type):
    """Map an AMS tray type (as the printer reports it) to one of the materials we have profiles for."""
    t = (slot_type or "").upper().strip()
    t = TYPE_ALIAS.get(t, t)
    return t if t in FILAMENTS else MATERIAL


def die(msg):
    print("ERROR:", msg)
    sys.exit(1)


# ---- STL reading --------------------------------------------------------------------
def read_stl(path):
    """Return a list of triangles ((x,y,z),(x,y,z),(x,y,z)) from a binary or ASCII STL."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * n == len(data):
            tris = []
            for i in range(n):
                v = struct.unpack("<12fH", data[84 + 50 * i:134 + 50 * i])
                tris.append(((v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11])))
            return tris
    pts = [tuple(map(float, m)) for m in re.findall(
        r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data.decode("utf-8", errors="replace"))]
    return [(pts[i], pts[i + 1], pts[i + 2]) for i in range(0, len(pts) - 2, 3)]


# ---- 3MF reading (only used when a .3mf is uploaded) ---------------------------------
import xml.etree.ElementTree as ET

NS_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
NS_PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


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
    out = []
    for r in range(4):
        row = a[r * 3:r * 3 + 3]
        x, y, z = row
        base = [x * b[0] + y * b[3] + z * b[6], x * b[1] + y * b[4] + z * b[7], x * b[2] + y * b[5] + z * b[8]]
        if r == 3:
            base = [base[0] + b[9], base[1] + b[10], base[2] + b[11]]
        out.extend(base)
    return out


def threemf_to_stl(src, dst):
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
    for item in root.find(NS_CORE + "build").iter(NS_CORE + "item"):
        emit(item.get(NS_PROD + "path") or main, item.get("objectid"), _matrix(item.get("transform")))
    if not tris:
        raise RuntimeError("no triangles found in the 3mf")
    with open(dst, "wb") as f:
        f.write(b"converted from 3mf".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            f.write(struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0))
    return len(tris)


# ---- color changes ------------------------------------------------------------------
def parse_changes(text):
    """Returns (base_slot, [(height_mm, slot)], [slot colors], [slot types])."""
    base, changes, colors = 1, [], ["#e53935", "#1e88e5", "#43a047", "#fdd835"]
    types = ["", "", "", ""]
    for part in [p.strip() for p in text.split(";") if p.strip()]:
        if part.startswith("base="):
            base = int(part[5:])
        elif part.startswith("slots="):
            colors = part[6:].split(",")
        elif part.startswith("types="):
            types = (part[6:].split(",") + ["", "", "", ""])[:4]
            if not all(re.fullmatch(r"[A-Za-z0-9+ -]{0,12}", t) for t in types):
                die(f"bad slot types: {types}")
        else:
            m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3})?):([1-4])", part)
            if not m:
                die(f"bad color change entry: {part!r}")
            changes.append((float(m.group(1)), int(m.group(2))))
    if not 1 <= base <= 4:
        die("base slot must be 1 to 4")
    if len(colors) != 4 or not all(re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in colors):
        die(f"bad slot colors: {colors}")
    if len(changes) > 30:
        die("too many color changes (max 30)")
    return base, sorted(changes), [c.lower() for c in colors], types


def layer_zs(first, height, top):
    zs, z = [], first
    while z <= top + 0.5:
        zs.append(round(z, 3))
        z += height
    return zs


def build_3mf(tris, dst, changes, colors, slots, variant, zs, with_settings, settings=None):
    """Write a Bambu-style project 3mf: one object, per-layer filament changes."""
    xs = [p[0] for t in tris for p in t]
    ys = [p[1] for t in tris for p in t]
    zs_all = [p[2] for t in tris for p in t]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    tx, ty, tz = 128 - cx, 128 - cy, -min(zs_all)

    index, verts, faces = {}, [], []
    for t in tris:
        ids = []
        for p in t:
            key = (round(p[0], 4), round(p[1], 4), round(p[2], 4))
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            ids.append(index[key])
        if len({*ids}) == 3:
            faces.append(ids)

    vtext = "\n".join(f'     <vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    ftext = "\n".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    model_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">BambuStudio-02.00.00.00</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <resources>
  <object id="1" type="model">
   <mesh>
    <vertices>
{vtext}
    </vertices>
    <triangles>
{ftext}
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 {tx} {ty} {tz}" printable="1"/>
 </build>
</model>
'''
    model_settings = '''<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="1">
    <metadata key="name" value="model"/>
    <metadata key="extruder" value="1"/>
    <part id="1" subtype="normal_part">
      <metadata key="name" value="model"/>
      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>
      <metadata key="source_file" value="model.stl"/>
    </part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    <model_instance>
      <metadata key="object_id" value="1"/>
      <metadata key="instance_id" value="0"/>
      <metadata key="identify_id" value="1"/>
    </model_instance>
  </plate>
</config>
'''
    type_code, gcode_attr, mode = variant
    layer_lines = []
    for z, slot in changes:
        layer_lines.append(
            f'<layer top_z="{z}" type="{type_code}" extruder="{slot}" color="{colors[slot - 1].upper()}" '
            f'extra="" gcode="{gcode_attr}"/>')
    custom = ('<?xml version="1.0" encoding="UTF-8"?>\n<custom_gcodes_per_layer>\n<plate>\n<plate_info id="1"/>\n'
              + "\n".join(layer_lines) + f'\n<mode value="{mode}"/>\n</plate>\n</custom_gcodes_per_layer>\n')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                     ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                     ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                     ' <Default Extension="png" ContentType="image/png"/>\n'
                     ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n</Relationships>\n')
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model_xml)
        z.writestr("Metadata/model_settings.config", model_settings)
        z.writestr("Metadata/custom_gcode_per_layer.xml", custom)
        if with_settings and settings is not None:
            z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=2))


def merged_project_settings(machine, process, filaments, slots, colors):
    """One project config: machine + process + one entry per filament in every per-filament list."""
    cfg = {}
    cfg.update(machine)
    cfg.update(process)
    keys = set()
    for f in filaments:
        keys.update(f.keys())
    for key in keys:
        vals = [f.get(key) for f in filaments]
        if all(isinstance(v, list) and len(v) == 1 for v in vals):
            cfg[key] = [v[0] for v in vals]
        elif key not in cfg:
            cfg[key] = vals[0]
    cfg["filament_colour"] = [colors[sl - 1] for sl in slots]
    cfg["from"] = "project"
    cfg["name"] = "project_settings"
    return cfg


def swap_evidence(gcode_text):
    """Which filaments does the G-code switch between? Looks for T<n> and M620 S<n>A."""
    tools = set(int(m) for m in re.findall(r"(?m)^T(\d{1,2})\b", gcode_text) if int(m) < 16)
    tools |= set(int(m) for m in re.findall(r"(?m)^M620 S(\d{1,3})A", gcode_text) if int(m) < 16)
    return tools


# ---- profiles -----------------------------------------------------------------------
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
    if CHANGES:
        parse_changes(CHANGES)


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


# ---- running the slicer ---------------------------------------------------------------
def run_slicer(label, out, work, final_name, settings_arg, filament_args, model, extra):
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    cmd = (["xvfb-run", "-a", "squashfs-root/AppRun", "--debug", "3"] + extra + [
        "--load-settings", settings_arg,
        "--load-filaments", filament_args,
        "--outputdir", str(out.resolve()),
        "--export-3mf", final_name,
        model])
    print(f"\n===== {label} =====")
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
    return found[0] if found else None


def read_gcode(result):
    with zipfile.ZipFile(result) as z:
        names = z.namelist()
        g = next((n for n in names if n.startswith("Metadata/") and n.endswith(".gcode")), None)
        if g is None:
            print("zip contents:", names)
            return None
        return z.read(g).decode("utf-8", errors="replace")


def main():
    check_inputs()
    root = find_profiles()
    index = build_index(root)
    print(f"{len(index)} profiles indexed under {root}")

    work = Path("work")
    out = Path("out")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()

    base_slot, raw_changes, colors, types = parse_changes(CHANGES) if CHANGES else (1, [], [], ["", "", "", ""])
    if CHANGES:
        used_slots = {base_slot} | {sl for _, sl in raw_changes}
        used_materials = {material_for(types[sl - 1]) for sl in used_slots}
    else:
        used_materials = {MATERIAL}
    print("materials in this print:", sorted(used_materials))

    machine = flatten(pick([MACHINE], index, "printer"), index)
    machine["printer_settings_id"] = machine["name"]

    process = flatten(pick([PROCESSES[LAYER]], index, "process"), index)
    process["print_settings_id"] = process["name"]
    process["layer_height"] = LAYER
    process["sparse_infill_density"] = f"{INFILL}%"
    # smaller G-code means a faster upload to the printer
    process["enable_arc_fitting"] = "1"
    process["resolution"] = "0.05"
    if "ABS" in used_materials:
        # no enclosure on the A1: a wide brim keeps the corners from lifting
        process["brim_type"] = "outer_only"
        process["brim_width"] = "8"
        process["brim_object_gap"] = "0"

    filament = flatten(pick(FILAMENTS[MATERIAL], index, "filament"), index)
    filament["filament_settings_id"] = [filament["name"]]

    print("machine :", machine["name"])
    print("process :", process["name"], f"(layer {LAYER}, infill {INFILL}%)")
    print("filament:", filament["name"])

    def write_profiles(compat, origin, tag="", colour=None, fil=None):
        paths = {}
        for kind, base_data in (("machine", machine), ("process", process), ("filament", fil or filament)):
            data = json.loads(json.dumps(base_data))
            data["from"] = origin
            if kind != "machine":
                if compat == "list":
                    data["compatible_printers"] = [machine["name"]]
                    data["compatible_printers_condition"] = ""
                elif compat == "none":
                    data.pop("compatible_printers", None)
                    data.pop("compatible_printers_condition", None)
            if kind == "filament" and colour:
                data["filament_colour"] = [colour]
            p = work / f"{kind}{tag}.json"
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            paths[kind] = str(p.resolve())
        return paths

    base = re.sub(r"\.(stl|3mf)$", "", Path(MODEL).name, flags=re.I)[:40]

    # the plain mesh as an STL
    if MODEL.lower().endswith(".3mf"):
        stl = work / "converted.stl"
        print(f"converted the 3mf to STL: {threemf_to_stl(MODEL, stl)} triangles")
        stl_path = str(stl.resolve())
    else:
        stl_path = str(Path(MODEL).resolve())

    # ---- find the printer-compatibility mode that this slicer accepts (single filament) ----
    final_single = f"{base}_{MATERIAL}.gcode.3mf"
    combos = [("list", "User"), ("none", "User"), ("asis", "User"), ("list", "system")]
    working, result = None, None
    for compat, origin in combos:
        paths = write_profiles(compat, origin)
        result = run_slicer(f"attempt: compatible_printers={compat}, from={origin}", out, work, final_single,
                            f"{paths['machine']};{paths['process']}", paths["filament"], stl_path,
                            ["--arrange", "1", "--slice", "0"])
        if result:
            working = (compat, origin)
            break
    if result is None:
        die("the slicer produced no .3mf file in any attempt; see the log above")

    if not CHANGES:
        finish(result, final_single)
        return

    # ---- several filaments: build a project with a filament change at each chosen layer ----
    first = float(process.get("initial_layer_print_height", LAYER) or LAYER)
    tris = read_stl(stl_path)
    top = max(p[2] for t in tris for p in t) - min(p[2] for t in tris for p in t)
    zs = layer_zs(first, float(LAYER), top)
    changes, last = [], base_slot
    for h, slot in raw_changes:
        z = min(zs, key=lambda v: abs(v - h))
        if slot != last:
            changes.append((z, slot))
            last = slot
    used = sorted({base_slot} | {s for _, s in changes})
    n_fil = max(used)
    print(f"base slot {base_slot}; changes (snapped to real layers): {changes}; filaments in project: {n_fil}")
    if not changes:
        print("no real color changes after snapping; using the single-filament result")
        finish(result, final_single)
        return

    compat, origin = working
    slot_filaments = []
    for i in range(1, n_fil + 1):
        mat = material_for(types[i - 1])
        slot_filaments.append(flatten(pick(FILAMENTS[mat], index, "filament"), index))
        print(f"slot {i}: {types[i - 1] or '(not reported)'} -> {slot_filaments[-1]['name']}")
    fpaths = [write_profiles(compat, origin, tag=f"_{i}", colour=colors[i - 1], fil=slot_filaments[i - 1])["filament"]
              for i in range(1, n_fil + 1)]
    ppaths = write_profiles(compat, origin)
    settings_arg = f"{ppaths['machine']};{ppaths['process']}"
    filament_arg = ";".join(fpaths)
    slot_list = list(range(1, n_fil + 1))
    for f in slot_filaments:
        f["filament_settings_id"] = [f["name"]]
    project = merged_project_settings(machine, process, slot_filaments, slot_list, colors)

    variants = [
        (2, "tool_change", "MultiAsSingle"),
        (0, "color_change", "MultiAsSingle"),
        (2, "tool_change", "SingleExtruder"),
    ]
    final_multi = f"{base}_{MATERIAL}_AMS{n_fil}.gcode.3mf"
    chosen = None
    for with_settings in (False, True):
        for variant in variants:
            proj = work / "multi.3mf"
            build_3mf(tris, proj, changes, colors, slot_list, variant, zs, with_settings, project)
            label = f"multi-filament: type={variant[0]} gcode={variant[1]} mode={variant[2]} project_settings={with_settings}"
            res = run_slicer(label, out, work, final_multi, settings_arg, filament_arg, str(proj.resolve()),
                             ["--slice", "0"])
            if not res:
                continue
            text = read_gcode(res)
            if text is None:
                continue
            tools = swap_evidence(text)
            print("filaments switched to in the G-code:", sorted(tools))
            if len(tools) >= 2:
                chosen = res
                break
        if chosen:
            break

    if chosen is None:
        die("could not build a multi-filament slice in any variant; see the log above "
            "(the single-color result was NOT saved, so you do not print the wrong thing)")
    finish(chosen, final_multi)


def finish(result, final_name):
    with zipfile.ZipFile(result) as z:
        names = z.namelist()
        print("largest files in the result (compressed bytes):")
        for i in sorted(z.infolist(), key=lambda i: -i.compress_size)[:6]:
            print(f"   {i.compress_size:>9}  {i.filename}")
    if not any(n.endswith(".gcode") and n.startswith("Metadata/") for n in names):
        print("zip contents:", names)
        die("the result has no Metadata/plate_*.gcode, so it is not a sliced file")
    Path("models").mkdir(exist_ok=True)
    dest = Path("models") / final_name
    shutil.copy(result, dest)
    print(f"OK: {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
