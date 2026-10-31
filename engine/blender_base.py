"""Blender subprocess: import data only, select the body, export embedded GLB."""
import argparse
import json
from pathlib import Path
import sys
import bpy


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--inspect",action="store_true")
    parser.add_argument("--textures")
    parser.add_argument("--object")
    args=parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source=Path(args.input)
    if source.suffix.lower()==".blend":
        bpy.ops.wm.open_mainfile(filepath=str(source),load_ui=False,use_scripts=False)
    else:
        bpy.ops.import_scene.fbx(filepath=str(source),use_anim=False,use_image_search=False)
    meshes=[o for o in bpy.context.scene.objects if o.type=="MESH"]
    summary=[{"name":o.name,"vertices":len(o.data.vertices),"polygons":len(o.data.polygons),
              "materials":[m.name if m else "" for m in o.data.materials],
              "rig":o.find_armature().name if o.find_armature() else None} for o in meshes]
    if args.inspect:
        Path(args.output).write_text(json.dumps({"meshes":summary,"images":[Path(i.filepath.replace('\\','/')).name for i in bpy.data.images if i.filepath]},indent=2));return
    if args.textures:
        for image in bpy.data.images:
            filename=Path(image.filepath.replace('\\','/')).name
            if filename and (Path(args.textures)/filename).is_file():
                image.filepath=str(Path(args.textures)/filename);image.reload()
    skinned=[o for o in meshes if o.find_armature()]
    pool=skinned or meshes
    named=[o for o in pool if any(t in o.name.lower() for t in ["body","base","nude"])
           and not any(t in o.name.lower() for t in ["cloth","outfit","shirt","dress"])]
    if not named:
        if len(pool)!=1:raise ValueError("Base ambigua nel FBX: esporta solo il corpo con lo scheletro.")
        named=pool
    body=max(named,key=lambda o:len(o.data.polygons));rig=body.find_armature()
    if args.object:
        body=next((o for o in pool if o.name==args.object),None)
        if body is None:raise ValueError("Variante della base non trovata")
        rig=body.find_armature()
    kept=[body]+[o for o in skinned if o!=body and o.find_armature()==rig
                 and any(t in o.name.lower() for t in ["head","eyes","eye_","teeth","mouth"])
                 and not any(t in o.name.lower() for t in ["lens","normal"])]
    # A source can include several wardrobe variants: only keep selected body
    # surfaces, never join arbitrary scene objects into the playable character.
    denied=["shirt","pants","dress","outfit","harness","collar","underwear","bra_","jacket","socks","lens"]
    for o in kept:
        if o.data.shape_keys:
            import sys
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            from facial import canonical
            for key in list(o.data.shape_keys.key_blocks)[1:]:
                if canonical(key.name) is None:o.shape_key_remove(key)
            if o.data.shape_keys:
                for key in o.data.shape_keys.key_blocks:key.value=0
        bad={i for i,m in enumerate(o.data.materials) if m and any(t in m.name.lower() for t in denied)}
        if bad:
            import bmesh
            bm=bmesh.new();bm.from_mesh(o.data)
            bmesh.ops.delete(bm,geom=[f for f in bm.faces if f.material_index in bad],context="FACES")
            bm.to_mesh(o.data);bm.free()
    bpy.ops.object.select_all(action="DESELECT")
    for o in kept:o.select_set(True)
    bpy.context.view_layer.objects.active=body
    if len(kept)>1:bpy.ops.object.join()
    if rig:rig.select_set(True)
    for o in [o for o in [body,rig] if o]:
        if o.animation_data:o.animation_data_clear()
    bpy.ops.export_scene.gltf(filepath=args.output,export_format="GLB",use_selection=True,
        export_animations=False,export_morph=True,export_skins=True,export_all_influences=False,
        export_def_bones=False,export_yup=True,export_materials="EXPORT")
    Path(args.output+".selection.json").write_text(json.dumps({"base":body.name,"objects":summary},indent=2))


main()
