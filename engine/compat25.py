"""Build local full-resource overrides while ArchiveXL is unavailable on 2.5.

Only the chosen avatar is included. Native game resources are extracted locally
and retain their gameplay data, cooked handles and original resource references.
"""
from pathlib import Path
import copy
import hashlib
import json
import shutil
from collections import Counter

import bridge as b
import cp77 as c
import library
import physics
import yaml

VERSION = 2
SUPPORTED_EXE = "d36e74c47592e9c656ae250078cb40b2be63c782af5bfbeff2280d43e653fab4"
RUNTIME = Path(__file__).parent / "runtime/AvatarBridge25.asi"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def supported(game):
    return digest(Path(game) / "bin/x64/Cyberpunk2077.exe") == SUPPORTED_EXE


def apply_components(native, changes):
    """Update both CR2W views, preserving aliases to cooked handles."""
    cooked = (native.get("compiledData") or {}).get("Data")
    if not cooked or "Chunks" not in cooked or "CruidDict" not in cooked:
        raise ValueError("Risorsa nativa senza componenti compilati: conversione interrotta")
    components = native.get("components", [])
    chunks = cooked["Chunks"]
    # Find unused handles rather than assuming a fixed range is free.
    used = set()
    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"HandleId", "HandleRefId"}: used.add(int(child))
                else: collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(native)
    next_handle = max(used | {29999}) + 1
    def handles(value, references=False):
        nonlocal next_handle
        if isinstance(value, dict):
            if "HandleId" in value and "Data" in value:
                if references:
                    return {"HandleRefId": value["HandleId"]}
                value["HandleId"] = str(next_handle)
                next_handle += 1
            return {key: handles(child, references) for key, child in value.items()}
        if isinstance(value, list):
            return [handles(child, references) for child in value]
        return value
    for change in changes:
        existing = next((item for item in components if item.get("id") == change.get("id")), None)
        if existing is not None:
            for group in (components, chunks):
                for item in group:
                    if item.get("id") == change.get("id"):
                        # Partial updates keep mesh/morph references: the 2.5
                        # garment assembler dereferences them even when hidden.
                        item.update(copy.deepcopy(change))
        else:
            addition = handles(copy.deepcopy(change))
            cooked["CruidDict"][str(len(chunks))] = str(addition["id"])
            chunks.append(addition)
            components.append(handles(copy.deepcopy(addition), references=True))
    native["components"] = components


def catalog(job, info):
    report = json.loads((job / "report.json").read_text(encoding="utf-8"))
    item = library.catalog_entry(job, report)
    item["arms"] = item["body"] + "_arms_fpp"
    item["coverage"] = info.get("coverage", {})
    item["compactPelvis"] = info.get("pelvis_profile_version", 0) >= 1
    item["intactSkin"] = info.get("intact_skin_version", 0) >= 1
    item["face"] = info.get("face")
    garments = info.get("garments", {})
    item["coverageGarments"] = library.clothing_coverage_hashes(garments, item["compactPelvis"], item["intactSkin"])
    item["coverageChunkCounts"] = library.clothing_chunk_counts(garments)
    item["lowerGarments"] = library.lower_garment_hashes(garments)
    item["hiddenFootwear"] = library.footwear_hashes(garments)
    if info.get("tail"):
        item["tail"] = "avatarbridge_" + job.name.removeprefix("avatar_") + "_tail"
        item["tailRig"] = item["tail"] + "_rig"
    return item


def avatar_components(item, info, perspective):
    result = []
    for part in (["body"] if perspective == "fpp" else ["body", "head"]):
        mesh = "body_fpp" if perspective == "fpp" else part
        result.append(library.body_component(item, part, info["resource_prefix"], True, mesh))
    if perspective == "fpp":
        arms = library.body_component(item, "arms", info["resource_prefix"], True, "arms_fpp")
        arms["renderingPlaneAnimationParam"] = c.name("renderPlane")
        result.append(arms)
    if item.get("tail"):
        tail = library.body_component(item, "tail", info["resource_prefix"], True)
        tail["skinning"]["Data"]["bindName"] = c.name(item["tailRig"])
        identifier = 1800000000000000000 + int(hashlib.sha256((item["id"] + "rig").encode()).hexdigest()[:14], 16)
        result.extend([physics.animated_component(item["tailRig"], info["resource_prefix"], identifier, True), tail])
    return result


def read_resource(directory, filename):
    matches = list(directory.rglob(filename + ".json"))
    if len(matches) != 1:
        raise ValueError(f"Risorsa mancante o ambigua: {filename}")
    return json.loads(matches[0].read_text(encoding="utf-8-sig"))


def checked_relative(value):
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.drive:
        raise ValueError("Percorso risorsa non valido")
    return relative


def native_document(originals, cache, relative, duplicate_names):
    if relative.name not in duplicate_names:
        return read_resource(cache, relative.name)
    # WolvenKit flattens directory conversions. Equal filenames in different
    # native directories must be serialized separately instead of overwriting.
    directory = cache / "distinct" / relative.parent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (relative.name + ".json")
    if not path.is_file():
        return c.serialize(originals / relative, directory)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_player(binary, item, directory):
    short = "ma" if item["gender"] == "male" else "wa"
    directory.mkdir(parents=True, exist_ok=True)
    document = c.serialize(binary / f"player_{short}_fpp.ent", directory)
    root = document["Data"]["RootChunk"]
    names = {component.get("name", {}).get("$value") for component in root.get("components", [])}
    if (root.get("entity", {}).get("Data", {}).get("$type") != "PlayerPuppet"
            or not {item["body"], item["arms"]}.issubset(names)
            or item["head"] in names):
        raise ValueError("La verifica della risorsa giocabile non è riuscita")
    return {"entity": "PlayerPuppet", "components": len(root["components"]),
            "compiled_chunks": len(root["compiledData"]["Data"]["Chunks"])}


def build(job, game):
    job, game = Path(job).resolve(), Path(game).resolve()
    if not job.is_relative_to((b.DATA / "jobs").resolve()):
        raise ValueError("Lavoro fuori dalla libreria locale")
    if not supported(game) or not RUNTIME.is_file():
        raise ValueError("Il componente di compatibilita non corrisponde a questa versione del gioco")
    info = json.loads((job / "build.json").read_text(encoding="utf-8"))
    package = Path(info["package"]).resolve()
    if not package.is_relative_to(job) or digest(package / (c.MOD + ".archive")) != info["sha256"]:
        raise ValueError("Pacchetto avatar modificato o fuori dalla libreria")
    key = hashlib.sha256((str(VERSION) + SUPPORTED_EXE + info["sha256"]).encode()).hexdigest()[:20]
    working = job / ("compat25_" + key)
    manifest = working / "build.json"
    if manifest.is_file():
        cached = json.loads(manifest.read_text(encoding="utf-8"))
        if all((working / "package" / name).is_file() and digest(working / "package" / name) == sha
               for name, sha in cached["package_hashes"].items()):
            # Names/categories can change without rebuilding geometry.
            write_script(working / "package/init.lua", catalog(job, info))
            shutil.copy2(RUNTIME, working / "package" / RUNTIME.name)
            cached["package_hashes"]["init.lua"] = digest(working / "package/init.lua")
            cached["package_hashes"][RUNTIME.name] = digest(RUNTIME)
            manifest.write_text(json.dumps(cached, indent=2), encoding="utf-8")
            return cached
    working.mkdir(parents=True, exist_ok=True)
    resources = working / "resources"
    resources.mkdir(exist_ok=True)
    c.command("unbundle", package / (c.MOD + ".archive"), "-o", resources,
              "-r", r"\.(mesh|xbm|app|rig|animgraph|ent)$")
    config = yaml.safe_load((package / (c.MOD + ".archive.xl")).read_text(encoding="utf-8"))
    reference = b.DATA / "compat-reference" / SUPPORTED_EXE[:16] / info["gender"]
    originals, original_json = reference / "originals", reference / "json"
    originals.mkdir(parents=True, exist_ok=True)
    original_json.mkdir(exist_ok=True)
    if not (reference / "ready.json").is_file():
        short = "ma" if info["gender"] == "male" else "wa"
        pattern = (r"(player_base_bodies\\appearances\\.*\.app$|player_base_heads\\appearances\\.*\.app$|"
                   + rf"entities\\player\\.*player_{short}_.*\.ent$)")
        c.command("unbundle", game / "archive/pc/content", "-o", originals, "-r", pattern)
        expansion = game / "archive/pc/ep1"
        if expansion.is_dir():
            c.command("unbundle", expansion, "-o", originals, "-r", pattern)
        for extension in ("app", "ent"):
            c.command("convert", "serialize", originals, "-o", original_json, "-w", "*." + extension)
        (reference / "ready.json").write_text(json.dumps({"exe": SUPPORTED_EXE}), encoding="utf-8")
    patch_json = working / "patch-json"
    patch_json.mkdir(exist_ok=True)
    c.command("convert", "serialize", resources, "-o", patch_json, "-w", "*.app")
    cooked, binary = working / "cooked", working / "cooked-bin"
    cooked.mkdir(exist_ok=True); binary.mkdir(exist_ok=True)
    changed = []
    item = catalog(job, info)
    counts = Counter(path.name for path in originals.rglob("*.*") if path.suffix in {".app", ".ent"})
    duplicate_names = {name for name, count in counts.items() if count > 1}
    for source, destinations in config["resource"]["patch"].items():
        patch = read_resource(patch_json, Path(source).name)["Data"]["RootChunk"] if source.endswith(".app") else None
        for destination in destinations:
            relative = checked_relative(destination)
            if not (originals / relative).is_file():
                if destination.startswith("ep1\\") and not (game / "archive/pc/ep1").is_dir():
                    continue
                raise ValueError(f"Risorsa del gioco non trovata: {destination}")
            document = native_document(originals, original_json, relative, duplicate_names)
            native = document["Data"]["RootChunk"]
            if relative.suffix == ".ent":
                changes = [c.disabled_component(comp) for comp in native.get("components", []) if c.original_surface(comp)]
                changes += avatar_components(item, info, "fpp" if "fpp" in relative.name else "tpp")
                apply_components(native, changes)
            else:
                patches = {value["Data"]["name"]["$value"]: value["Data"] for value in patch["appearances"]}
                for appearance in native["appearances"]:
                    definition = appearance["Data"]
                    change = patches.get(definition["name"]["$value"])
                    if change: apply_components(definition, change["components"])
            target = cooked / relative.with_suffix(relative.suffix + ".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document), encoding="utf-8")
            changed.append(relative)
    if len(changed) < 5:
        raise ValueError("Risorse del giocatore incomplete")
    c.command("convert", "deserialize", cooked, "-o", binary, "-w", "*.json")
    changed_counts = Counter(relative.name for relative in changed)
    for relative in changed:
        if changed_counts[relative.name] > 1:
            directory = binary / "distinct" / relative.parent
            directory.mkdir(parents=True, exist_ok=True)
            c.command("convert", "deserialize", cooked / relative.with_suffix(relative.suffix + ".json"), "-o", directory)
    validation = validate_player(binary, item, working / "verification")
    archive = working / "archive"
    archive.mkdir(exist_ok=True)
    for source in resources.rglob("*"):
        if source.is_file() and source.suffix in {".mesh", ".xbm", ".app", ".rig", ".animgraph"}:
            target = archive / source.relative_to(resources)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for relative in changed:
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source = (binary / "distinct" / relative) if changed_counts[relative.name] > 1 else (binary / relative.name)
        shutil.copy2(source, target)
    for garment in info.get("garments", {}).values():
        source = archive / checked_relative(garment["mesh"])
        target = archive / checked_relative(garment["source"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    destination = working / "package"
    destination.mkdir(exist_ok=True)
    c.command("pack", archive, "-o", destination)
    (destination / "archive.archive").replace(destination / (c.MOD + ".archive"))
    # An empty config prevents a stale ArchiveXL patch from being applied later.
    (destination / (c.MOD + ".archive.xl")).write_text("resource: {}\n", encoding="utf-8")
    write_script(destination / "init.lua", item)
    shutil.copy2(RUNTIME, destination / RUNTIME.name)
    result = {"stage": "compiled", "mode": "compat25", "compat_version": VERSION,
              "package": str(destination), "sha256": digest(destination / (c.MOD + ".archive")),
              "package_hashes": {p.name: digest(p) for p in destination.iterdir() if p.is_file()},
              "selected_avatar": job.name, "native_overrides": len(changed),
              "native_validation": validation,
              "garment_overrides": len(info.get("garments", {})), "runtime_verified": False,
              "limitations": ["Cambiare avatar nell'app a gioco chiuso e reinstallare.",
                              "La modalita compatibilita non abilita le altre mod RED4ext."]}
    manifest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def write_script(path, item):
    template = Path(__file__).with_name("compat25.lua").read_text(encoding="utf-8")
    Path(path).write_text(template.replace("CATALOG_JSON", library.lua_literal(item)), encoding="utf-8")
