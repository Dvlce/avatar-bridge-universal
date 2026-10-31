"""Local avatar workspace. Never connects to a network or executes imported code."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CyberpunkAvatarBridge"
MODEL_EXT = {".asset", ".fbx", ".glb", ".gltf", ".vrm", ".blend", ".obj"}
DENIED = {"library", "packages", "temp", "obj", "logs", ".git", ".vs", "node_modules"}


def run(args, timeout=600):
    result = subprocess.run([str(a) for a in args], capture_output=True,
                            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("Codice " + str(result.returncode) + ": " +
                         (result.stderr + result.stdout).decode("utf-8", "replace")[-7000:])
    return result.stdout


def sevenzip():
    for p in [Path(__file__).resolve().parent.parent / "tools/7zip/7z.exe",
              Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe",
              Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7za.exe"]:
        if p.is_file():
            return p
    raise ValueError("Installa 7-Zip per leggere RAR e ZIP. I modelli non vengono inviati online.")


def clean_entry(name):
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    if any(ord(c) < 32 for c in name) or p.is_absolute() or not p.parts or any(x in {"..", "."} or ":" in x for x in p.parts):
        raise ValueError("Percorso non sicuro nell'archivio")
    return str(p)


def eligible(name):
    p = PurePosixPath(name.replace("\\", "/"))
    return not any(part.lower() in DENIED for part in p.parts)


class Source:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.entries = {}
        if not self.path.exists():
            raise ValueError("Sorgente non trovata")
        self.archive = self.path.is_file() and self.path.suffix.lower() in {".rar", ".zip", ".7z"}
        if self.archive:
            if re.search(r"\.part(?!0*1\.)\d+\.rar$", self.path.name, re.I):
                raise ValueError("Per un RAR diviso seleziona part1.rar; lascia tutte le parti nella stessa cartella.")
            listing = run([sevenzip(), "l", "-slt", "-sccUTF-8", "--", self.path]).decode("utf-8-sig", "replace").replace("\r\n", "\n")
            for block in listing.split("\n\n"):
                row = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
                if "Path" not in row or "Size" not in row or row.get("Folder") == "+":
                    continue
                if any(row.get(k) for k in ("Symbolic Link", "Hard Link", "Copy Link")):
                    continue
                try:
                    key = clean_entry(row["Path"])
                except ValueError:
                    continue
                self.entries[key] = {"size": int(row["Size"]), "original": row["Path"]}
        elif self.path.is_dir():
            for base, dirs, files in os.walk(self.path, followlinks=False):
                dirs[:] = [d for d in dirs if d.lower() not in DENIED and not (Path(base) / d).is_symlink()
                           and not (Path(base) / d).is_junction()]
                for f in files:
                    p = Path(base) / f
                    if not p.is_symlink() and p.resolve().is_relative_to(self.path):
                        self.entries[p.relative_to(self.path).as_posix()] = {"size": p.stat().st_size}
        else:
            if self.path.suffix.lower() not in MODEL_EXT:
                raise ValueError("Trascina una cartella, un archivio RAR/ZIP/7z o un modello. Un EXE non è un avatar.")
            self.entries[self.path.name] = {"size": self.path.stat().st_size}
            # Adjacent metadata and a prefab are needed by serialized Unity meshes.
            for p in self.path.parent.glob("*.meta"):
                self.entries[p.name] = {"size": p.stat().st_size}
            for p in self.path.parent.glob("*.prefab"):
                self.entries[p.name] = {"size": p.stat().st_size}
        self.cache = {}

    def prefetch(self, names):
        """Batch small metadata reads, keeping untrusted scripts out of the cache."""
        names = [clean_entry(n) for n in names if n not in self.cache]
        if not self.archive or not names:
            return
        if any(not n.endswith(".meta") for n in names) or sum(self.entries[n]["size"] for n in names) > 64 * 1024 * 1024:
            raise ValueError("Metadati troppo grandi")
        DATA.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="metadata_", dir=DATA) as temp:
            root = Path(temp)
            listing = root / "selection.txt"
            listing.write_text("\n".join(self.entries[n]["original"] for n in names), encoding="utf-8")
            dest = root / "files"
            dest.mkdir()
            run([sevenzip(), "x", "-y", "-bd", "-spd", "-scsUTF-8", "-o" + str(dest),
                 str(self.path), "@" + str(listing)])
            for name in names:
                p = dest / name
                if not p.is_symlink() and p.resolve().is_relative_to(dest) and p.is_file():
                    self.cache[name] = p.read_bytes()

    def read(self, name, max_size=256 * 1024 * 1024):
        name = clean_entry(name)
        if name not in self.entries or self.entries[name]["size"] > max_size:
            raise ValueError("File assente o troppo grande per questa operazione")
        if name in self.cache:
            return self.cache[name]
        if self.archive:
            value = run([sevenzip(), "e", "-so", "-bd", "-spd", "--", self.path, self.entries[name]["original"]])
        else:
            root = self.path if self.path.is_dir() else self.path.parent
            p = (root / name).resolve()
            if not p.is_relative_to(root.resolve()):
                raise ValueError("Percorso fuori dalla sorgente")
            value = p.read_bytes()
        if len(value) != self.entries[name]["size"]:
            raise ValueError("Dimensione del file inattesa: controlla l'integrità dell'archivio")
        if len(value) < 8 * 1024 * 1024:
            self.cache[name] = value
        return value


def base_score(name):
    stem = PurePosixPath(name).stem.lower()
    score = 100 if stem == "body_1" else 80 if stem in {"body", "base", "rexouium"} else 45 if "body" in stem else 0
    if PurePosixPath(name).suffix.lower()==".blend":score+=20
    if any(s in name.lower() for s in ["clothing", "hood", "collar", "harness", "demo", "fishnet", "suit", "shader"]):
        score -= 100
    return score


def scan(path):
    source = Source(path)
    candidates = [{"entry": name, "size": row["size"], "score": base_score(name),
                   "format": PurePosixPath(name).suffix.lower()}
                  for name, row in source.entries.items()
                  if eligible(name) and PurePosixPath(name).suffix.lower() in MODEL_EXT]
    candidates.sort(key=lambda x: (-x["score"], x["entry"]))
    if source.path.is_dir():
        for entry in source.entries:
            if Path(entry).suffix.lower() not in {".rar", ".zip", ".7z"} or not eligible(entry):
                continue
            if re.search(r"\.part(?!0*1\.)\d+\.rar$", entry, re.I):
                continue
            nested = scan(source.path/entry)
            for candidate in nested["candidates"]:
                candidate["source"] = str(source.path/entry)
                candidates.append(candidate)
        candidates.sort(key=lambda x: (-x["score"], x.get("source", ""), x["entry"]))
    return {"source": str(source.path), "files": len(source.entries), "candidates": candidates,
            "privacy": "Solo PC. Nessun caricamento. Nessuno script dell'avatar viene eseguito."}


def humanoid_name(value):
    """Common Unity/VRM/Mixamo/Blender labels -> a shared humanoid profile."""
    n = re.sub(r"[^a-z0-9]", "", value.lower().split(":")[-1])
    for prefix in ["mixamorig", "armature", "bip001", "bip01"]:
        n = n.removeprefix(prefix)
    middle = {"hips":"Hips", "pelvis":"Hips", "spine":"Spine", "spine1":"Chest",
              "spine2":"UpperChest", "chest":"Chest", "upperchest":"UpperChest",
              "hip":"Hips", "neck":"Neck", "neck1":"Neck", "head":"Head", "jaw":"Jaw",
              "eyel":"Eye_L", "lefteye":"Eye_L", "eyer":"Eye_R", "righteye":"Eye_R"}
    if n in middle: return middle[n]
    if n in {"tailrt","tailroot"}:return "Tail_Rt"
    tail=re.fullmatch(r"tail(\d+)",n)
    if tail:return "Tail"+str(int(tail[1]))
    for side,letter in [("Left","l"),("Right","r")]:
        parts = {"shoulder":"shoulder", "clavicle":"shoulder", "arm":"arm", "upperarm":"arm",
                 "elbow":"elbow", "forearm":"elbow", "lowerarm":"elbow", "wrist":"wrist", "hand":"wrist",
                 "leg":"leg", "thigh":"leg", "upleg":"leg", "upperleg":"leg", "knee":"knee",
                 "calf":"knee", "lowerleg":"knee", "ankle":"ankle", "foot":"ankle", "toe":"toe", "toes":"toe", "toebase":"toe"}
        for part,target in parts.items():
            if n in {side.lower()+part,letter+part,part+letter}:
                return side+" "+target
        for finger in ["Thumb","Index","Middle","Ring","Pinky"]:
            aliases=[finger.lower()] + (["little"] if finger=="Pinky" else [])
            for i,label in [(1,"proximal"),(2,"intermediate"),(3,"distal")]:
                for f in aliases:
                    if n in {f+f"finger{i}"+letter,side.lower()+"hand"+f+str(i),side.lower()+f+str(i),f+str(i)+letter,side.lower()+f+label}:
                        return finger+f"Finger{i}_"+side[0]
    return None


def normalize_humanoid(path, overrides=None):
    doc=glb_doc(path);data=Path(path).read_bytes();length=struct.unpack_from("<I",data,12)[0]
    mapped={}
    extensions=doc.get("extensions",{})
    for item in extensions.get("VRM",{}).get("humanoid",{}).get("humanBones",[]):
        mapped[item["node"]]=item["bone"]
    for semantic,item in extensions.get("VRMC_vrm",{}).get("humanoid",{}).get("humanBones",{}).items():
        mapped[item["node"]]=semantic
    aliases={};seen=set()
    for skin in doc.get("skins",[]):
        for index in skin["joints"]:
            if index in seen:continue
            seen.add(index);node=doc["nodes"][index];old=node.get("name","")
            semantic=(overrides or {}).get(old,mapped.get(index,old))
            canonical=humanoid_name(semantic)
            if canonical:
                if canonical in aliases.values():
                    raise ValueError("Ossa ambigue nel profilo umanoide: "+canonical+". Specifica il mapping in avatarbridge.json.")
                aliases[old]=canonical;node["name"]=canonical
    writer=GLB();writer.doc=doc;writer.bin=bytearray(data[28+length:]);writer.save(path)
    return aliases


def yaml_load(text):
    import yaml
    return yaml.load(text, Loader=yaml.CSafeLoader)


def unity_docs(raw, types=None):
    text = raw.decode("utf-8-sig")
    docs = {}
    for match in re.finditer(r"^--- !u!(\d+) &(-?\d+)[^\n]*\n(.*?)(?=^--- !u!|\Z)", text, re.M | re.S):
        if types is None or int(match[1]) in types:
            docs[int(match[2])] = yaml_load(match[3])
    return docs


def read_mesh(raw):
    text = raw.decode("utf-8-sig")
    # Neutral shape only. Morph target buffers can be much larger than the mesh.
    text = re.sub(r"^  m_Shapes:.*?(?=^  m_BindPose:)", "", text, flags=re.M | re.S)
    docs = unity_docs(text.encode(), {43})
    if len(docs) != 1:
        raise ValueError("Supportata una mesh Unity YAML per file .asset; i file Unity binari richiedono un export FBX/GLB.")
    mesh = next(iter(docs.values()))["Mesh"]
    if mesh.get("m_StreamData", {}).get("size", 0):
        raise ValueError("Questa mesh usa un buffer esterno: esportala da Unity in FBX o GLB.")
    return mesh


def vertex_channels(mesh):
    import numpy as np
    vd = mesh["m_VertexData"]
    count, channels = vd["m_VertexCount"], vd["m_Channels"]
    formats = {0: ("<f4", 4), 1: ("<f2", 2), 2: ("u1", 1), 3: ("i1", 1),
               4: ("<u2", 2), 5: ("<i2", 2), 6: ("u1", 1), 7: ("i1", 1),
               8: ("<u2", 2), 9: ("<i2", 2), 10: ("<u4", 4), 11: ("<i4", 4)}
    raw = bytes.fromhex(str(vd["_typelessdata"]))
    strides = {}
    for ch in channels:
        if ch["dimension"]:
            if ch["format"] not in formats:
                raise ValueError("Formato di vertice Unity non supportato")
            stride = ch["offset"] + formats[ch["format"]][1] * ch["dimension"]
            strides[ch["stream"]] = max(strides.get(ch["stream"], 0), stride)
    starts, cursor = {}, 0
    for stream, stride in sorted(strides.items()):
        starts[stream] = cursor
        cursor = (cursor + stride * count + 15) & ~15
    result = {}
    for i, ch in enumerate(channels):
        if not ch["dimension"]:
            continue
        dtype, size = formats[ch["format"]]
        arr = np.ndarray((count, ch["dimension"]), dtype=dtype, buffer=raw,
                         offset=starts[ch["stream"]] + ch["offset"], strides=(strides[ch["stream"]], size)).copy()
        if ch["format"] in (2, 4):
            arr = arr.astype(np.float32) / (255 if size == 1 else 65535)
        if ch["format"] in (3, 5):
            arr = np.maximum(arr.astype(np.float32) / (127 if size == 1 else 32767), -1)
        result[i] = arr
    return result


class GLB:
    def __init__(self):
        self.doc = {"asset": {"version": "2.0", "generator": "Cyberpunk Avatar Bridge (local)"},
                    "buffers": [{"byteLength": 0}], "bufferViews": [], "accessors": [],
                    "meshes": [], "nodes": [], "materials": [], "scenes": [{"nodes": []}], "scene": 0}
        self.bin = bytearray()

    def blob(self, value, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(value)}
        if target:
            view["target"] = target
        self.bin.extend(value)
        self.doc["bufferViews"].append(view)
        return len(self.doc["bufferViews"]) - 1

    def array(self, arr, kind, component=5126, bounds=False, target=None):
        import numpy as np
        dtype = {5126: "<f4", 5125: "<u4", 5123: "<u2"}[component]
        arr = np.ascontiguousarray(arr, dtype=dtype)
        acc = {"bufferView": self.blob(arr.tobytes(), target), "componentType": component,
               "count": len(arr), "type": kind}
        if bounds:
            acc["min"], acc["max"] = arr.min(axis=0).tolist(), arr.max(axis=0).tolist()
        self.doc["accessors"].append(acc)
        return len(self.doc["accessors"]) - 1

    def save(self, path):
        while len(self.bin) % 4:
            self.bin.append(0)
        self.doc["buffers"][0]["byteLength"] = len(self.bin)
        encoded = json.dumps(self.doc, separators=(",", ":"), allow_nan=False).encode()
        encoded += b" " * (-len(encoded) % 4)
        length = 12 + 8 + len(encoded) + 8 + len(self.bin)
        Path(path).write_bytes(struct.pack("<4sII", b"glTF", 2, length) + struct.pack("<I4s", len(encoded), b"JSON") +
                               encoded + struct.pack("<I4s", len(self.bin), b"BIN\0") + self.bin)


def find_renderer(source, entry):
    meta = entry + ".meta"
    if meta not in source.entries:
        raise ValueError("Serve anche il .asset.meta originale per associare lo scheletro.")
    guid = yaml_load(source.read(meta).decode())["guid"]
    candidates = [n for n in source.entries if eligible(n) and n.lower().endswith(".prefab")]
    # Prefer the extracted Unity model hierarchy over a fully configured VRChat variant.
    candidates.sort(key=lambda n: (0 if "/Assets/Assets/" in n else 1, n.count("/"), n))
    for name in candidates:
        raw = source.read(name, 32 * 1024 * 1024)
        if str(guid).encode() not in raw:
            continue
        docs = unity_docs(raw, {1, 4, 137})
        for value in docs.values():
            renderer = value.get("SkinnedMeshRenderer")
            if renderer and renderer.get("m_Mesh", {}).get("guid") == guid:
                return docs, renderer, name
    raise ValueError("Nessun prefab contiene lo scheletro di questa mesh. Aggiungi il prefab originale oppure esporta un GLB con rig.")


def material_data(source, renderer):
    """Read only .mat files referenced by this body's renderer. Never load Unity shaders."""
    refs = [str(x.get("guid", "")) for x in renderer.get("m_Materials", [])]
    found = {}
    metas = [n for n in source.entries if eligible(n) and n.lower().endswith(".mat.meta")]
    source.prefetch(metas)
    for name in metas:
        raw = source.read(name, 1024 * 1024).decode("utf-8-sig", "replace")
        match = re.search(r"^guid: (\w+)", raw, re.M)
        if match and match[1] in refs:
            docs = unity_docs(source.read(name[:-5]), {21})
            if docs:
                material = next(iter(docs.values()))["Material"]
                found[match[1]] = {"name": material.get("m_Name", PurePosixPath(name).stem),
                                  "source": name[:-5], "data": material}
        if all(g in found for g in refs):
            break
    return [found.get(g, {"name": f"base_material_{i:02}", "data": {}}) for i, g in enumerate(refs)]


def texture_refs(materials):
    result = set()
    for mat in materials:
        envs = mat["data"].get("m_SavedProperties", {}).get("m_TexEnvs", [])
        for item in envs:
            if "_MainTex" in item:
                guid = item["_MainTex"].get("m_Texture", {}).get("guid")
                if guid:
                    mat["albedo_guid"] = str(guid)
                    result.add(str(guid))
    return result


def resolve_textures(source, wanted):
    found = {}
    if not wanted:
        return found
    metas = [n for n in source.entries if eligible(n) and
             PurePosixPath(n[:-5]).suffix.lower() in {".png", ".jpg", ".jpeg"} and n.endswith(".meta")]
    source.prefetch(metas)
    for name in metas:
        match = re.search(rb"^guid: (\w+)", source.read(name, 1024 * 1024), re.M)
        if match and match[1].decode() in wanted:
            found[match[1].decode()] = name[:-5]
        if len(found) == len(wanted):
            break
    return found


def unity_to_glb(source, entry, output):
    import numpy as np
    original_mesh = source.read(entry)
    mesh = read_mesh(original_mesh)
    channels = vertex_channels(mesh)
    docs, renderer, prefab = find_renderer(source, entry)
    mats = material_data(source, renderer)
    textures = resolve_textures(source, texture_refs(mats))
    glb = GLB()
    for mat in mats:
        material = {"name": mat["name"], "doubleSided": True,
                    "pbrMetallicRoughness": {"metallicFactor": 0, "roughnessFactor": 0.8}}
        if mat.get("albedo_guid") in textures:
            path = textures[mat["albedo_guid"]]
            image_id = len(glb.doc.setdefault("images", []))
            glb.doc["images"].append({"bufferView": glb.blob(source.read(path, 64 * 1024 * 1024)),
                                      "mimeType": "image/png" if path.lower().endswith(".png") else "image/jpeg"})
            glb.doc.setdefault("textures", []).append({"source": image_id})
            material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": image_id}
        glb.doc["materials"].append(material)
    positions = channels[0].astype(np.float32)
    positions[:, 0] *= -1  # Unity left-handed -> glTF right-handed.
    attrs = {"POSITION": glb.array(positions, "VEC3", bounds=True, target=34962)}
    if 1 in channels:
        normals = channels[1].astype(np.float32)
        normals[:, 0] *= -1
        attrs["NORMAL"] = glb.array(normals, "VEC3", target=34962)
    if 4 in channels:
        uv = channels[4].astype(np.float32)
        uv[:, 1] = 1 - uv[:, 1]
        attrs["TEXCOORD_0"] = glb.array(uv, "VEC2", target=34962)
    poses = mesh.get("m_BindPose", [])
    bone_ids = [x["fileID"] for x in renderer["m_Bones"]]
    if len(poses) != len(bone_ids) or 12 not in channels or 13 not in channels:
        raise ValueError("Pesi e scheletro della mesh non coincidono: export interrotto.")
    joints = channels[13].astype(np.uint16)
    weights = channels[12].astype(np.float32)
    sums = weights.sum(axis=1)
    if np.any(sums <= 0) or np.any(channels[13] >= len(poses)):
        raise ValueError("Pesi o indici delle ossa non validi")
    weights /= sums[:, None]
    attrs["JOINTS_0"] = glb.array(joints, "VEC4", 5123, target=34962)
    attrs["WEIGHTS_0"] = glb.array(weights, "VEC4", target=34962)
    mirror = np.diag([-1., 1., 1., 1.])
    ibms = np.array([[[b[f"e{r}{c}"] for c in range(4)] for r in range(4)] for b in poses])
    ibms = mirror @ ibms @ mirror
    globals_ = np.linalg.inv(ibms)
    by_id = {fid: i for i, fid in enumerate(bone_ids)}
    roots = []
    for i, fid in enumerate(bone_ids):
        transform = docs.get(fid, {}).get("Transform", {})
        gameobject = docs.get(transform.get("m_GameObject", {}).get("fileID"), {}).get("GameObject", {})
        ancestor = transform.get("m_Father", {}).get("fileID", 0)
        seen = {fid}
        while ancestor and ancestor not in by_id:
            if ancestor in seen:
                raise ValueError("Ciclo nella gerarchia dello scheletro")
            seen.add(ancestor)
            ancestor = docs.get(ancestor, {}).get("Transform", {}).get("m_Father", {}).get("fileID", 0)
        local = np.linalg.inv(globals_[by_id[ancestor]]) @ globals_[i] if ancestor in by_id else globals_[i]
        node = {"name": gameobject.get("m_Name", f"bone_{i}"), "matrix": local.T.reshape(-1).tolist()}
        glb.doc["nodes"].append(node)
        if ancestor in by_id:
            # Parents may appear after children in Unity's bone array.
            pass
        else:
            roots.append(i)
    for i, fid in enumerate(bone_ids):
        ancestor = docs.get(fid, {}).get("Transform", {}).get("m_Father", {}).get("fileID", 0)
        seen = {fid}
        while ancestor and ancestor not in by_id:
            if ancestor in seen:
                raise ValueError("Ciclo nella gerarchia")
            seen.add(ancestor)
            ancestor = docs.get(ancestor, {}).get("Transform", {}).get("m_Father", {}).get("fileID", 0)
        if ancestor in by_id:
            glb.doc["nodes"][by_id[ancestor]].setdefault("children", []).append(i)
    ibm_acc = glb.array(ibms.transpose(0, 2, 1).reshape(-1, 16), "MAT4")
    glb.doc["skins"] = [{"name": "Base_avatar_rig", "joints": list(range(len(poses))), "inverseBindMatrices": ibm_acc}]
    index_bytes = bytes.fromhex(str(mesh["m_IndexBuffer"]))
    dtype = "<u4" if mesh.get("m_IndexFormat", 0) == 1 else "<u2"
    indices = np.frombuffer(index_bytes, dtype=dtype)
    primitives, submeshes = [], []
    for i, sub in enumerate(mesh["m_SubMeshes"]):
        if i < len(mats) and "eyelens" in mats[i]["name"].lower():
            continue  # Unity's transparent specular overlay must not cover the original iris.
        if sub["topology"] != 0 or sub["indexCount"] % 3:
            raise ValueError("La base deve contenere solo triangoli")
        start = sub["firstByte"] // np.dtype(dtype).itemsize
        idx = indices[start:start + sub["indexCount"]].astype(np.uint32) + sub.get("baseVertex", 0)
        if len(idx) != sub["indexCount"] or (len(idx) and idx.max() >= len(positions)):
            raise ValueError("Indici di triangoli fuori intervallo")
        idx = idx.reshape(-1, 3)[:, [0, 2, 1]].reshape(-1)
        if not len(idx):
            continue
        primitive = {"attributes": attrs.copy(), "indices": glb.array(idx, "SCALAR", 5125, target=34963), "mode": 4}
        if i < len(mats):
            primitive["material"] = i
        primitives.append(primitive)
        submeshes.append({"index": i, "triangles": len(idx) // 3,
                          "material": mats[i]["name"] if i < len(mats) else "unknown"})
    from facial import add_unity_targets
    facial_names=add_unity_targets(original_mesh,len(positions),glb,primitives)
    glb.doc["meshes"].append({"name": "Base_avatar", "primitives": primitives,
                            "extras":{"targetNames":facial_names},"weights":[0.]*len(facial_names)})
    mesh_node = len(glb.doc["nodes"])
    glb.doc["nodes"].append({"name": "Base_avatar", "mesh": 0, "skin": 0})
    # Preserve renderer orientation (the actual base is Z-up inside the Unity mesh).
    tr = next((d["Transform"] for d in docs.values() if "Transform" in d and
               d["Transform"].get("m_GameObject") == renderer.get("m_GameObject")), {})
    q = tr.get("m_LocalRotation", {"x": 0, "y": 0, "z": 0, "w": 1})
    pos = tr.get("m_LocalPosition", {"x": 0, "y": 0, "z": 0})
    scale = tr.get("m_LocalScale", {"x": 1, "y": 1, "z": 1})
    glb.doc["nodes"].append({"name": "Avatar_base_only", "children": roots + [mesh_node],
                             "rotation": [q["x"], -q["y"], -q["z"], q["w"]],
                             "translation": [-pos["x"], pos["y"], pos["z"]],
                             "scale": [scale["x"], scale["y"], scale["z"]]})
    glb.doc["scenes"][0]["nodes"] = [len(glb.doc["nodes"]) - 1]
    glb.save(output)
    return {"vertices": len(positions), "triangles": sum(s["triangles"] for s in submeshes),
            "bones": len(poses), "submeshes": submeshes, "prefab": prefab,
            "textures_found": len(textures), "textures_expected": len(texture_refs(mats)),
            "bone_names": [n["name"] for n in glb.doc["nodes"][:len(poses)]]}


def glb_doc(path):
    data = Path(path).read_bytes()
    if len(data) < 20 or data[:4] != b"glTF" or struct.unpack_from("<I", data, 4)[0] != 2:
        raise ValueError("File GLB/VRM non valido")
    if struct.unpack_from("<I", data, 8)[0] != len(data) or data[16:20] != b"JSON":
        raise ValueError("Contenitore GLB incompleto")
    size = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + size])
    if any("uri" in b for b in doc.get("buffers", [])) or any("uri" in i for i in doc.get("images", [])):
        raise ValueError("Usa un GLB con buffer e texture incorporati; i riferimenti esterni non vengono aperti.")
    start = 20 + size
    if start + 8 > len(data) or data[start + 4:start + 8] != b"BIN\0":
        raise ValueError("Buffer binario GLB assente")
    binary_size = struct.unpack_from("<I", data, start)[0]
    if start + 8 + binary_size != len(data) or len(doc.get("buffers", [])) != 1 or doc["buffers"][0]["byteLength"] > binary_size:
        raise ValueError("Dimensione del buffer GLB non valida")
    for view in doc.get("bufferViews", []):
        if view.get("buffer", 0) != 0 or view.get("byteOffset", 0) < 0 or view.get("byteLength", -1) < 0 or view.get("byteOffset", 0) + view["byteLength"] > binary_size:
            raise ValueError("Buffer view GLB fuori intervallo")
    nodes = doc.get("nodes", [])
    if len(nodes) > 10000:
        raise ValueError("Troppi nodi per questa anteprima")
    parents = {}
    for index, node in enumerate(nodes):
        for child in node.get("children", []):
            if not isinstance(child, int) or child < 0 or child >= len(nodes) or child in parents:
                raise ValueError("Gerarchia GLB non valida")
            parents[child] = index
    for index in range(len(nodes)):
        seen = set()
        current = index
        while current in parents:
            if current in seen or len(seen) > 250:
                raise ValueError("Gerarchia GLB ciclica o troppo profonda")
            seen.add(current)
            current = parents[current]
    component_sizes = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    dimensions = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
    for acc in doc.get("accessors", []):
        if "sparse" in acc or acc.get("componentType") not in component_sizes or acc.get("type") not in dimensions:
            raise ValueError("Accessore GLB non supportato: esporta un GLB standard non compresso")
        if "bufferView" not in acc or not 0 <= acc["bufferView"] < len(doc.get("bufferViews", [])):
            raise ValueError("Accessore senza buffer valido")
        view = doc["bufferViews"][acc["bufferView"]]
        width = component_sizes[acc["componentType"]] * dimensions[acc["type"]]
        stride = view.get("byteStride", width)
        count = acc.get("count", -1)
        if count < 0 or count > 10000000 or stride < width or acc.get("byteOffset", 0) < 0:
            raise ValueError("Dimensione accessore GLB non valida")
        if count and acc.get("byteOffset", 0) + (count - 1) * stride + width > view["byteLength"]:
            raise ValueError("Accessore GLB fuori intervallo")
    for skin in doc.get("skins", []):
        if any(not isinstance(j, int) or not 0 <= j < len(nodes) for j in skin.get("joints", [])):
            raise ValueError("Osso GLB fuori intervallo")
    return doc


def accessor_array(doc, binary, index):
    import numpy as np
    a = doc["accessors"][index]
    view = doc["bufferViews"][a["bufferView"]]
    types = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
    dims = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
    dtype, dim = np.dtype(types[a["componentType"]]), dims[a["type"]]
    values = np.ndarray((a["count"], dim), dtype=dtype, buffer=binary,
                        offset=view.get("byteOffset", 0) + a.get("byteOffset", 0),
                        strides=(view.get("byteStride", dtype.itemsize * dim), dtype.itemsize)).copy()
    if a.get("normalized") and a["componentType"] != 5126:
        values = values.astype(np.float32) / np.iinfo(dtype).max
    return values


def node_matrix(node):
    import numpy as np
    if "matrix" in node:
        return np.array(node["matrix"]).reshape(4, 4).T
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    matrix = np.array([[1-2*y*y-2*z*z, 2*x*y-2*z*w, 2*x*z+2*y*w, 0],
                       [2*x*y+2*z*w, 1-2*x*x-2*z*z, 2*y*z-2*x*w, 0],
                       [2*x*z-2*y*w, 2*y*z+2*x*w, 1-2*x*x-2*y*y, 0], [0, 0, 0, 1.]])
    matrix[:3, :3] *= np.array(node.get("scale", [1, 1, 1]))[None, :]
    matrix[:3, 3] = node.get("translation", [0, 0, 0])
    return matrix


def reduce_rig(path, output):
    """Merge secondary weights into nearest retained ancestor, preserving the bind pose."""
    import numpy as np
    doc = glb_doc(path)
    if len(doc.get("skins", [])) != 1 or doc.get("animations"):
        raise ValueError("Il profilo leggero richiede una sola base con un rig e nessuna animazione.")
    file = Path(path).read_bytes()
    json_len = struct.unpack_from("<I", file, 12)[0]
    glb = GLB()
    glb.doc, glb.bin = doc, bytearray(file[20 + json_len + 8:])
    nodes, skin = doc["nodes"], doc["skins"][0]
    joints = skin["joints"]
    names = [re.sub(r"[^a-z0-9]", "", nodes[j].get("name", "").lower()) for j in joints]
    essential = {"hips", "spine", "chest", "upperchest", "neck", "head", "jaw", "eyel", "eyer"}
    for side in ("left", "right"):
        essential.update(side + part for part in ("shoulder", "arm", "elbow", "wrist", "leg", "knee", "ankle", "toe"))
    keep = {i for i, name in enumerate(names) if name in essential or re.fullmatch(r".*finger[123][lr]", name)}
    from physics import tail_chain
    anatomical_tail=set(tail_chain([nodes[j].get("name","") for j in joints]))
    keep.update(i for i,j in enumerate(joints) if nodes[j].get("name","") in anatomical_tail)
    if not {"hips", "head", "leftarm", "rightarm", "leftleg", "rightleg"}.issubset(set(names)) or len(keep) < 20:
        raise ValueError("Rig non riconosciuto per l'alleggerimento automatico. Disattiva il profilo leggero per conservarlo.")
    parents = {child: parent for parent, node in enumerate(nodes) for child in node.get("children", [])}
    by_node = {j: i for i, j in enumerate(joints)}
    mapped = []
    for i, joint in enumerate(joints):
        ancestor = joint
        visited = set()
        while ancestor not in by_node or by_node[ancestor] not in keep:
            if ancestor in visited or ancestor not in parents:
                raise ValueError("Osso secondario senza un genitore conservabile")
            visited.add(ancestor)
            ancestor = parents[ancestor]
        mapped.append(by_node[ancestor])
    kept = sorted(keep)
    dense = {old: new for new, old in enumerate(kept)}
    remap = np.array([dense[i] for i in mapped], dtype=np.uint16)
    updated = {}
    for mesh in doc["meshes"]:
        for primitive in mesh["primitives"]:
            attrs = primitive["attributes"]
            key = (attrs["JOINTS_0"], attrs["WEIGHTS_0"])
            if key not in updated:
                old_joints = accessor_array(doc, glb.bin, key[0]).astype(np.int32)
                old_weights = accessor_array(doc, glb.bin, key[1]).astype(np.float32)
                new_joints = remap[old_joints]
                new_weights = old_weights.copy()
                # Combine repeated influences before sorting and normalizing.
                for col in range(1, 4):
                    for prev in range(col):
                        same = new_joints[:, col] == new_joints[:, prev]
                        new_weights[same, prev] += new_weights[same, col]
                        new_weights[same, col] = 0
                order = np.argsort(-new_weights, axis=1)
                new_joints = np.take_along_axis(new_joints, order, axis=1)
                new_weights = np.take_along_axis(new_weights, order, axis=1)
                new_weights /= new_weights.sum(axis=1, keepdims=True)
                updated[key] = (glb.array(new_joints, "VEC4", 5123, target=34962), glb.array(new_weights, "VEC4", target=34962))
            attrs["JOINTS_0"], attrs["WEIGHTS_0"] = updated[key]
    ibms = accessor_array(doc, glb.bin, skin["inverseBindMatrices"])[kept]
    skin["inverseBindMatrices"] = glb.array(ibms, "MAT4")
    removed = {joints[i] for i in range(len(joints)) if i not in keep}
    globals_ = {}
    def world(index):
        if index not in globals_:
            globals_[index] = (world(parents[index]) if index in parents else np.eye(4)) @ node_matrix(nodes[index])
        return globals_[index]
    for index in range(len(nodes)):
        world(index)
    surviving = [i for i in range(len(nodes)) if i not in removed]
    indices = {old: new for new, old in enumerate(surviving)}
    new_nodes = []
    new_parents = {}
    for index in surviving:
        node = dict(nodes[index])
        parent = parents.get(index)
        while parent in removed:
            parent = parents.get(parent)
        if parent is not None:
            new_parents[indices[index]] = indices[parent]
        local = np.linalg.inv(globals_[parent]) @ globals_[index] if parent is not None else globals_[index]
        for key in ("children", "translation", "rotation", "scale"):
            node.pop(key, None)
        node["matrix"] = local.T.reshape(-1).tolist()
        new_nodes.append(node)
    for child, parent in new_parents.items():
        new_nodes[parent].setdefault("children", []).append(child)
    for scene in doc["scenes"]:
        scene["nodes"] = [indices[i] for i in scene["nodes"] if i in indices]
    skin["joints"] = [indices[joints[i]] for i in kept]
    if "skeleton" in skin:
        skin["skeleton"] = indices.get(skin["skeleton"], skin["joints"][0])
    doc["nodes"] = new_nodes
    glb.save(output)
    return {"bones_before": len(joints), "bones_after": len(kept),
            "merged_bones": [nodes[joints[i]]["name"] for i in range(len(joints)) if i not in keep]}


def geometry_stats(path):
    doc = glb_doc(path)
    position_accessors = {p["attributes"]["POSITION"] for m in doc["meshes"] for p in m["primitives"]}
    return {"vertices": sum(doc["accessors"][a]["count"] for a in position_accessors),
            "triangles": sum(doc["accessors"][p["indices"]]["count"] // 3 for m in doc["meshes"] for p in m["primitives"]),
            "bones": sum(len(s["joints"]) for s in doc.get("skins", [])),
            "materials": len(doc.get("materials", [])), "file_bytes": Path(path).stat().st_size}


def remove_eye_overlay(path,output):
    doc=glb_doc(path)
    data=Path(path).read_bytes();json_length=struct.unpack_from("<I",data,12)[0]
    excluded={i for i,m in enumerate(doc.get("materials",[])) if "eyelens" in m.get("name","").lower()}
    for mesh in doc["meshes"]:
        mesh["primitives"]=[p for p in mesh["primitives"] if p.get("material") not in excluded]
    writer=GLB();writer.doc=doc;writer.bin=bytearray(data[28+json_length:]);writer.save(output)
    return geometry_stats(output)


def optimize_base(path, output):
    tool = DATA / "tools/gltfpack/gltfpack.exe"
    if not tool.is_file(): tool = Path(__file__).resolve().parent.parent / "tools/gltfpack/gltfpack.exe"
    if not tool.is_file():
        raise ValueError("Manca gltfpack. Esegui setup.ps1 per installare l'ottimizzatore locale.")
    before = geometry_stats(path)
    if not glb_doc(path).get("skins"):
        run([tool,"-i",path,"-o",output,"-si","0.5","-se","0.01","-slb","-noq","-kn","-km"])
        return {"before":before,"after":geometry_stats(output),"rig":{"bones_before":0,"bones_after":0},
                "note":"Modello senza scheletro: disponibile per anteprima, non per sostituire V."}
    staged = Path(output).with_name("base-rig-reduced.glb")
    rig_info = reduce_rig(path, staged)
    # No compression/quantization extensions: readable by WPF and WolvenKit.
    # Border locks protect seams. 1% geometric error limit avoids forcing destructive reductions.
    run([tool, "-i", staged, "-o", output, "-si", "0.5", "-se", "0.01", "-slb", "-noq", "-kn", "-km"])
    after = geometry_stats(output)
    return {"before": before, "after": after, "rig": rig_info,
            "note": "Movimento umanoide e dita conservati; la catena anatomica della coda viene mantenuta per la fisica nel gioco. Le altre ossa secondarie seguono il genitore. La resa nel gioco richiede una verifica separata."}


def fbx_to_glb(source,entry,output):
    blender=next((DATA/"tools/blender").glob("*/blender.exe"),None)
    if blender is None:
        raise ValueError("Per FBX serve Blender locale. Esegui setup.ps1 -WithBlender.")
    stage=Path(output).parent/"fbx-input";stage.mkdir()
    fbx=stage/PurePosixPath(entry).name;fbx.write_bytes(source.read(entry,max_size=512*1024*1024))
    script=Path(__file__).with_name("blender_base.py");inspection=stage/"inspect.json"
    common=[blender,"--background","--factory-startup","--disable-autoexec","--python",script,"--","--input",fbx]
    run([*common,"--output",inspection,"--inspect"])
    if not inspection.is_file():raise ValueError("Blender non ha letto il modello FBX")
    inspected=json.loads(inspection.read_text())
    textures=stage/"textures";textures.mkdir()
    for filename in set(inspected.get("images",[])):
        if not filename or Path(filename).suffix.lower() not in {".png",".jpg",".jpeg"}:continue
        matches=[n for n in source.entries if PurePosixPath(n).name.lower()==filename.lower()]
        if len(matches)==1:(textures/filename).write_bytes(source.read(matches[0]))
    run([*common,"--output",output,"--textures",textures])
    if not Path(output).is_file():raise ValueError("Base FBX non esportata: controlla il modello e lo scheletro")
    return geometry_stats(output)


def refresh_tail_profile(report,job):
    """Recover tail joints removed by older lightweight exports, using local base only."""
    from physics import tail_chain
    if not report.get('optimization'):return report
    base=Path(report.get('base_output',Path(job)/'base-avatar.glb')).resolve()
    if not base.is_file() or not base.is_relative_to((DATA/'jobs').resolve()):return report
    def tails(path):
        doc=glb_doc(path)
        return tail_chain([doc['nodes'][j].get('name','') for skin in doc.get('skins',[]) for j in skin['joints']])
    if len(tails(base))<2 or len(tails(report['output']))>=len(tails(base)):return report
    neutral=Path(job)/'base-neutral.glb';remove_eye_overlay(base,neutral)
    output=Path(job)/'base-avatar-tail.glb'
    report['optimization']=optimize_base(neutral,output)
    report['output']=str(output);report['base_output']=str(neutral)
    report['geometry']=geometry_stats(output);report['sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
    (Path(job)/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    return report


def refresh_face_profile(report,job):
    """Upgrade a cached neutral Unity export using its local original source."""
    if report.get('face_profile_version',0)>=4:return report
    if not report.get('selected_base','').lower().endswith('.asset'):return report
    job=Path(job);base=job/'base-with-face.glb'
    info=unity_to_glb(Source(report['source']),report['selected_base'],base)
    from geometry import remove_dorsal_crest
    clean=job/'base-face-clean.glb';removed=remove_dorsal_crest(base,clean)
    output=clean
    if report.get('optimization'):
        output=job/'base-face-light.glb';report['optimization']=optimize_base(clean,output)
    report.update(output=str(output),base_output=str(clean),face_profile_version=4,
                  removed_dorsal_crest_triangles=removed,geometry=geometry_stats(output),
                  sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    (job/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    return report


def prepare(path, entry, gender="female", lightweight=True):
    if gender not in {"female", "male"}:
        raise ValueError("Scegli V femminile o maschile")
    source = Source(path)
    entry = clean_entry(entry)
    if not eligible(entry) or entry not in source.entries:
        raise ValueError("Seleziona un modello presente nella sorgente")
    job = DATA / "jobs" / ("avatar_" + uuid.uuid4().hex[:12])
    job.mkdir(parents=True)
    output = job / "base-avatar.glb"
    ext = PurePosixPath(entry).suffix.lower()
    if ext == ".asset":
        info = unity_to_glb(source, entry, output)
    elif ext in {".fbx",".blend"}:
        info = fbx_to_glb(source,entry,output)
    elif ext in {".glb", ".vrm"}:
        output.write_bytes(source.read(entry))
        doc = glb_doc(output)
        info = {"meshes": len(doc.get("meshes", [])), "bones": sum(len(s["joints"]) for s in doc.get("skins", [])),
                "note": "GLB copiato: controllare in anteprima che includa solo la base."}
    else:
        raise ValueError("Per FBX/BLEND/OBJ: esporta SOLO la base come GLB incorporando il rig e le texture. Questa versione converte direttamente le mesh Unity YAML (.asset) e i GLB/VRM.")
    settings={}
    config_entry=next((n for n in source.entries if PurePosixPath(n).name=="avatarbridge.json"),None)
    if config_entry:
        settings=json.loads(source.read(config_entry,max_size=65536))
        if not isinstance(settings,dict) or not isinstance(settings.get("bones",{}),dict):
            raise ValueError("Profilo avatarbridge.json non valido")
    aliases=normalize_humanoid(output,settings.get("bones",{}))
    from geometry import remove_dorsal_crest
    removed_crest=remove_dorsal_crest(output,output)
    optimization = None
    if lightweight:
        light = job / "base-avatar-light.glb"
        optimization = optimize_base(output, light)
        output = light
    report = {"schema": 1, "stage": "base_exported", "playable": False, "target": "player_v", "gender": gender,
              "name":str(settings.get("name",PurePosixPath(entry).stem)),"category":str(settings.get("category","Avatar")),"humanoid_mapping":aliases,
              "source": str(source.path), "selected_base": entry, "output": str(output), "job": str(job),
              "base_output":str(job/"base-avatar.glb"),
              "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "geometry": geometry_stats(output),
              "face_profile_version":4,"removed_dorsal_crest_triangles":removed_crest,
              "original_geometry": info, "optimization": optimization,
              "remaining": ["Adattare posa, proporzioni e pesi allo scheletro di V selezionato.",
                            "Separare corpo, testa, braccia FPP e relative maschere; convertire i materiali REDengine.",
                            "Importare dentro mesh originali con WolvenKit e compilare un .archive.",
                            "Provare animazioni, prima persona e vestiti nel gioco; i vestiti possono richiedere refit."],
              "privacy": "Dati conservati sul PC. Nessun upload. Nessun contenuto aggiuntivo dell'avatar eseguito."}
    (job / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def windows_version_info(path):
    """Read PE version resources without loading or executing the file."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, buffer):
        return {}
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    result = {}
    if api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) and length.value >= 52:
        fixed = struct.unpack("<13I", ctypes.string_at(pointer, 52))
        if fixed[0] == 0xFEEF04BD:
            for key, high, low in (("FileVersion", fixed[2], fixed[3]),
                                   ("ProductVersion", fixed[4], fixed[5])):
                result[key] = f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"
    if not api.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        return result
    translations = ctypes.string_at(pointer, length.value)
    for offset in range(0, len(translations) - 3, 4):
        language, codepage = struct.unpack_from("<HH", translations, offset)
        for key in ("FileVersion", "ProductVersion"):
            query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            if api.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and length.value:
                result[key] = ctypes.wstring_at(pointer, length.value).rstrip("\0")
    return result


def runtime_status(game):
    """Local prerequisites; file presence never proves in-game compatibility."""
    game = Path(game)
    version = windows_version_info(game / "bin/x64/Cyberpunk2077.exe").get("ProductVersion")
    required = {
        "RED4ext loader": "bin/x64/winmm.dll",
        "RED4ext": "red4ext/RED4ext.dll",
        "ArchiveXL": "red4ext/plugins/ArchiveXL/ArchiveXL.dll",
        "Codeware": "red4ext/plugins/Codeware/Codeware.dll",
        "CET loader": "bin/x64/version.dll",
        "Cyber Engine Tweaks": "bin/x64/plugins/cyber_engine_tweaks.asi",
        "redscript config": "engine/config/base/scripts.ini",
        "redscript compiler": "engine/tools/scc.exe",
        "redscript library": "engine/tools/scc_lib.dll",
    }
    components = {label: {"path": relative, "present": (game / relative).is_file(),
                         "version": windows_version_info(game / relative).get("FileVersion")}
                  for label, relative in required.items()}
    problems = []
    missing = [label for label, item in components.items() if not item["present"]]
    archive_version = components["ArchiveXL"]["version"] or ""
    extender_version = components["RED4ext"]["version"] or ""
    # This exact release crashes during startup on the local 2.5 installation.
    # Do not infer support or incompatibility for future framework releases.
    numbers = lambda value: tuple(int(n) for n in re.findall(r"\d+", value or ""))
    if (numbers(version) in {(2, 5), (2, 50)} and numbers(archive_version)[:3] == (1, 27, 5)
            and numbers(extender_version)[:3] == (1, 30, 0)):
        problems.append("Cyberpunk 2.5: ArchiveXL 1.27.5 causa un arresto all'avvio su questa installazione. "
                        "Serve una versione compatibile prima di riattivare RED4ext.")
    if missing:
        problems.append("Dipendenze mancanti o disattivate: " + ", ".join(missing) + ".")
    mode = "native"
    can_install = not problems
    summary = " ".join(problems) if problems else "Dipendenze presenti. La resa del personaggio richiede una verifica nel gioco."
    if problems and numbers(version) in {(2, 5), (2, 50)}:
        import compat25
        if (not components["RED4ext loader"]["present"]
                and components["CET loader"]["present"] and components["Cyber Engine Tweaks"]["present"]
                and compat25.RUNTIME.is_file() and compat25.supported(game)):
            mode = "compat25"
            can_install = True
            summary = ("Cyberpunk 2.5: modalità compatibilità disponibile. Avatar, coda e vestiti adattati; "
                       "scegli il personaggio nell'app a gioco chiuso e premi Installa. "
                       "Le altre mod RED4ext restano disattivate.")
    return {"game_version": version, "components": components, "ready": not problems,
            "can_install": can_install, "mode": mode,
            "problems": problems, "runtime_verified": False,
            "summary": summary,
            "references": ["https://github.com/psiberx/cp2077-archive-xl/issues/30",
                           "https://github.com/wopss/RED4ext/issues/153"]}


def require_runtime(game):
    status = runtime_status(game)
    if not status["can_install"]:
        raise ValueError(status["summary"])
    return status


def inspect_game(path):
    game = Path(path).resolve()
    exe = game / "bin/x64/Cyberpunk2077.exe"
    if not exe.is_file():
        raise ValueError("Seleziona la cartella principale di Cyberpunk 2077")
    moddir = game / "archive/pc/mod"
    mods = sorted(p.name for p in moddir.glob("*.archive"))
    conflicts = [m for m in mods if any(s in m.lower() for s in ["body", "exotic", "head", "rig"])]
    return {"game": str(game), "valid": True, "redmod": (game / "tools/redmod/bin/redMod.exe").is_file(),
            "mods": mods, "appearance_mods": conflicts, "runtime": runtime_status(game),
            "note": "Le mod del corpo già presenti possono sostituire le stesse risorse. Nessuna mod esistente è stata cambiata."}


def create_project(job_path, game_path):
    job = Path(job_path).resolve()
    if not job.is_relative_to((DATA / "jobs").resolve()):
        raise ValueError("Seleziona un lavoro presente nella libreria locale")
    report = json.loads((job / "report.json").read_text(encoding="utf-8"))
    game = Path(inspect_game(game_path)["game"])
    cli = DATA / "tools/WolvenKit/WolvenKit.CLI.exe"
    if not cli.is_file():
        raise ValueError("WolvenKit CLI non trovato. Esegui setup.ps1 -WithWolvenKit.")
    project = job / "WolvenKit"
    if (project / "project-manifest.json").is_file():
        return {"project_dir": str(project), "existing": True}
    archive = project / "source/archive"
    raw = project / "source/raw"
    resources = project / "source/resources"
    for p in [archive, raw, resources, project / "reference"]:
        p.mkdir(parents=True, exist_ok=True)
    gender = report["gender"]
    code = "pwa" if gender == "female" else "pma"
    rig = "woman" if gender == "female" else "man"
    pattern = rf"(t0_000_{code}_base__full\.mesh|a0_\d+_{code}_base_hq__full\.mesh|player_{rig}.*deformations\.rig)$"
    commands = []
    args = [cli, "unbundle", game / "archive/pc/content/basegame_4_appearance.archive",
            "-o", archive, "-r", pattern]
    log = run(args).decode("utf-8", "replace")
    commands.append(log)
    body_refs = list(archive.rglob(f"t0_000_{code}_base__full.mesh"))
    if len(body_refs) != 1:
        raise ValueError("WolvenKit non ha estratto la mesh di riferimento. Controlla il gioco e la versione del tool.")
    # Own portable CLI configuration: no user's installed WolvenKit settings are modified.
    settings = cli.parent / "appsettings.json"
    config = json.loads(settings.read_text(encoding="utf-8-sig"))
    config.setdefault("MeshExportArgs", {})["withMaterials"] = False
    settings.write_text(json.dumps(config, indent=2), encoding="utf-8")
    commands.append(run([cli, "export", archive, "-o", raw, "-gp", game]).decode("utf-8", "replace"))
    body_glb = raw / body_refs[0].relative_to(archive).with_suffix(".glb")
    if not body_glb.is_file():
        (project / "wolvenkit.log").write_text("\n".join(commands), encoding="utf-8")
        raise ValueError("Export della mesh Cyberpunk non riuscito. Il log è nella cartella del progetto.")
    shutil.copy2(Path(report["output"]), project / "reference/base-avatar.glb")
    name = "avatar_bridge_" + job.name.removeprefix("avatar_")
    xml = ET.Element("CP77Mod")
    for key, value in {"Name": name, "ModName": name, "Author": "", "Email": "",
                       "Description": "LOCAL WORK IN PROGRESS - avatar base adaptation. Not playable yet.", "Version": "0.1.0"}.items():
        ET.SubElement(xml, key).text = value
    ET.ElementTree(xml).write(project / (name + ".cpmodproj"), encoding="utf-8", xml_declaration=True)
    target_doc = glb_doc(body_glb)
    target_bones = [target_doc["nodes"][j].get("name", "") for skin in target_doc.get("skins", []) for j in skin["joints"]]
    source_doc = glb_doc(report["output"])
    source_bones = [source_doc["nodes"][j].get("name", "") for skin in source_doc.get("skins", []) for j in skin["joints"]]
    mapping = {"status": "manual_review_required", "target_rig": gender, "source_bones": source_bones,
               "target_bones": target_bones, "mapping": {},
               "note": "Nessuna corrispondenza viene applicata automaticamente: le ossa VRChat non sono il rig Cyberpunk."}
    (project / "bone-map.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    instructions = """# Adattamento della base a V — lavoro incompleto

Questo progetto contiene la base originale esportata e riferimenti estratti dal gioco locale.
Non contiene ancora un avatar giocabile. Compilare ora produrrebbe soltanto una copia di risorse originali.

1. Apri il .cpmodproj con WolvenKit. I file sotto source/archive sono contenitori originali.
2. Importa reference/base-avatar.glb in Blender. Non importare il progetto VRChat intero.
3. Importa la mesh di V sotto source/raw con il WolvenKit Blender IO Suite. Esporta il rig
   appropriato da WolvenKit con l'opzione WithRig per ottenere anche la gerarchia completa.
4. Adatta posa e proporzioni della base. Dividi corpo, testa, occhi e braccia in prima persona.
   Rimappa e trasferisci i pesi al rig di V: bone-map.json elenca le due liste, non applica un
   retarget automatico. Coda, orecchie e volto richiedono una scelta di rig/animazione dedicata.
5. Crea materiali REDengine e texture .xbm. Lo shader Poiyomi di Unity non è convertito.
6. Esporta ogni pezzo sopra il GLB corrispondente in source/raw. Usa submesh_00, submesh_01,
   triangolazione e massimo 65.534 triangoli per submesh. Reimporta nelle .mesh con WolvenKit.
7. Per avatar e stili come Biosculpted Exotics serve un'apparenza di V coerente con testa,
   corpo, occhi, FPP e maschere. La mod Exotics installata non viene copiata né ridistribuita.
8. Compila un .archive distinto solo dopo l'adattamento. Testa prima su un salvataggio di prova:
   movimento, armi, specchi, fotomodalità, cyberware, collo, equipaggiamento e inventario.
   I vestiti devono essere adattati alle proporzioni della nuova base per evitare compenetrazioni.

Le texture di anteprima nel GLB sono PBR di base, non una replica degli shader VRChat.
Le shape key facciali non sono esportate in questa versione. Non viene installato nulla nel gioco.

Fonti tecniche:
- https://wiki.redmodding.org/wolvenkit/wolvenkit-app/usage/import-export/models
- https://wiki.redmodding.org/cyberpunk-2077-modding/for-mod-creators-theory/modding-tools/wolvenkit-blender-io-suite/wkit-blender-plugin-import-export
- https://www.nexusmods.com/cyberpunk2077/mods/9797
"""
    (project / "LEGGIMI.md").write_text(instructions, encoding="utf-8")
    originals = {p.relative_to(archive).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in archive.rglob("*") if p.is_file()}
    manifest = {"stage": "reference_project", "playable": False, "project_dir": str(project),
                "original_resource_hashes": originals, "target_gender": gender,
                "source_sha256": report["sha256"], "appearance_mods": inspect_game(game_path)["appearance_mods"]}
    (project / "wolvenkit.log").write_text("\n".join(commands), encoding="utf-8")
    (project / "project-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["scan", "prepare", "game", "project", "build", "install", "uninstall", "label"])
    parser.add_argument("--source", required=True)
    parser.add_argument("--entry")
    parser.add_argument("--gender", default="female")
    parser.add_argument("--game")
    parser.add_argument("--name")
    parser.add_argument("--category")
    parser.add_argument("--target", choices=["cyberpunk", "gmod", "portable"], default="cyberpunk")
    parser.add_argument("--full", action="store_true", help="Conserva il rig e la geometria completi")
    args = parser.parse_args()
    try:
        if args.command == "label":
            job=Path(args.source).resolve()
            if not job.is_relative_to((DATA/"jobs").resolve()): raise ValueError("Lavoro fuori dalla libreria locale")
            p=job/"report.json";result=json.loads(p.read_text(encoding="utf-8"))
            result["name"]=(args.name or "Avatar").strip()[:80] or "Avatar"
            result["category"]=(args.category or "Avatar").strip()[:60] or "Avatar"
            p.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
        elif args.command == "scan":
            result = scan(args.source)
        elif args.command == "game":
            from targets import inspect_target
            result = inspect_target(args.target, args.source)
        elif args.command == "project":
            if not args.game:
                raise ValueError("Seleziona la cartella del gioco")
            result = create_project(args.source, args.game)
        elif args.command in {"build", "install", "uninstall"}:
            if not args.game:
                raise ValueError("Seleziona la cartella del gioco")
            from targets import execute
            result = execute(args.target, args.command, args.source, args.game)
        else:
            if not args.entry:
                raise ValueError("Seleziona la mesh base")
            result = prepare(args.source, args.entry, args.gender, lightweight=not args.full)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
