"""Local REDengine build and transactional installation. No network access."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import time
import uuid
import numpy as np
import bridge as b

MOD = "zz_avatarbridge"
PREFIX = "avatarbridge\\rexouium"


def name(value):
    return {"$type": "CName", "$storage": "string", "$value": value}


def ref(value):
    return {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": value}, "Flags": "Default"}


def cli():
    p = b.DATA / "tools/WolvenKit/WolvenKit.CLI.exe"
    if not p.is_file():
        raise ValueError("WolvenKit CLI non trovato")
    return p


def command(*args):
    text = b.run([cli(), *args]).decode("utf-8", "replace")
    if re.search(r"\[\s*\d+: Error\s*\]", text):
        raise ValueError(text[-5000:])
    return text


def serialize(p, dest):
    command("convert", "serialize", p, "-o", dest)
    out = Path(dest) / (Path(p).name + ".json")
    if not out.is_file():
        raise ValueError("Serializzazione WolvenKit non riuscita")
    return json.loads(out.read_text(encoding="utf-8-sig"))


def deserialize(doc, filename, dest):
    p = Path(dest) / (filename + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    command("convert", "deserialize", p, "-o", dest)
    output = Path(dest) / filename
    if not output.is_file() or output.read_bytes()[:4] != b"CR2W":
        raise ValueError("Risorsa REDengine non generata: " + filename)
    return output


def load_glb(path):
    doc = b.glb_doc(path)
    data = Path(path).read_bytes()
    return doc, data[28 + struct.unpack_from("<I", data, 12)[0]:]


def world_matrices(doc):
    nodes = doc["nodes"]
    parents = {ch: i for i, n in enumerate(nodes) for ch in n.get("children", [])}
    result = {}
    def world(i):
        if i not in result:
            result[i] = (world(parents[i]) if i in parents else np.eye(4)) @ b.node_matrix(nodes[i])
        return result[i]
    for i in range(len(nodes)):
        world(i)
    return result


def rig_matrices(doc):
    rig = doc["Data"]["RootChunk"]
    matrices = []
    conversion = np.array([[1., 0, 0, 0], [0, 0, 1., 0], [0, -1., 0, 0], [0, 0, 0, 1.]])
    for i, tr in enumerate(rig["boneTransforms"]):
        q, t, s = tr["Rotation"], tr["Translation"], tr["Scale"]
        local = b.node_matrix({"rotation": [q["i"], q["j"], q["k"], q["r"]],
                               "translation": [t["X"], t["Y"], t["Z"]], "scale": [s["X"], s["Y"], s["Z"]]})
        parent = rig["boneParentIndexes"][i]
        matrices.append((matrices[parent] if parent >= 0 else np.eye(4)) @ local)
    return {n["$value"]: conversion @ m @ np.linalg.inv(conversion) for n, m in zip(rig["boneNames"], matrices)}


def bone_alias(source):
    norm = re.sub(r"[^a-z0-9]", "", source.lower())
    direct = {"hips": "Hips", "spine": "Spine1", "chest": "Spine3", "upperchest": "Spine3", "neck": "Neck",
              "head": "Head", "jaw": "Head", "eyel": "Head", "eyer": "Head"}
    if norm in direct:
        return direct[norm]
    for side in ["Left", "Right"]:
        for old, new in {"shoulder": "Shoulder", "arm": "Arm", "elbow": "ForeArm", "wrist": "Hand",
                         "leg": "UpLeg", "knee": "Leg", "ankle": "Foot", "toe": "ToeBase"}.items():
            if norm == side.lower() + old:
                return side + new
    match = re.fullmatch(r"(thumb|index|middle|ring|pinky)finger([123])([lr])", norm)
    if match:
        finger, number, side = match.groups()
        if finger == "thumb":
            # Cyberpunk's thumb has an in-hand joint and two phalange joints.
            return ("Left" if side == "l" else "Right") + ("InHandThumb" if number == "1" else "HandThumb" + str(int(number)-1))
        return ("Left" if side == "l" else "Right") + "Hand" + finger.title() + number
    raise ValueError("Osso da adattare non riconosciuto: " + source)


def frame(direction):
    z = direction / max(np.linalg.norm(direction), 1e-8)
    forward = np.array([0., 0., -1.])
    if abs(z @ forward) > .9:
        forward = np.array([0., 1., 0.])
    x = np.cross(forward, z); x /= max(np.linalg.norm(x), 1e-8)
    return np.column_stack([x, np.cross(z, x), z])


def trs(matrix):
    """Emit explicit SRT; WolvenKit's bone importer cannot read matrix-only joints."""
    u,_,vt=np.linalg.svd(matrix[:3,:3]);rotation=u@vt
    if np.linalg.det(rotation)<0:
        u[:,-1]*=-1;rotation=u@vt
    m=rotation;trace=np.trace(m)
    if trace>0:
        s=np.sqrt(trace+1)*2
        q=[(m[2,1]-m[1,2])/s,(m[0,2]-m[2,0])/s,(m[1,0]-m[0,1])/s,.25*s]
    else:
        i=int(np.argmax(np.diag(m)));j=(i+1)%3;k=(i+2)%3
        s=np.sqrt(1+m[i,i]-m[j,j]-m[k,k])*2
        q=[0.,0.,0.,0.];q[i]=.25*s;q[j]=(m[j,i]+m[i,j])/s;q[k]=(m[k,i]+m[i,k])/s;q[3]=(m[k,j]-m[j,k])/s
    q=np.array(q);q/=np.linalg.norm(q)
    return {"translation":matrix[:3,3].tolist(),"rotation":q.tolist(),"scale":[1.,1.,1.]}


def tangents(positions,normals,uv,indices):
    triangles=indices.reshape(-1,3)
    edge1=positions[triangles[:,1]]-positions[triangles[:,0]]
    edge2=positions[triangles[:,2]]-positions[triangles[:,0]]
    duv1=uv[triangles[:,1]]-uv[triangles[:,0]]
    duv2=uv[triangles[:,2]]-uv[triangles[:,0]]
    det=duv1[:,0]*duv2[:,1]-duv1[:,1]*duv2[:,0]
    inv=np.divide(1.,det,out=np.zeros_like(det),where=np.abs(det)>1e-10)
    t=(edge1*duv2[:,1,None]-edge2*duv1[:,1,None])*inv[:,None]
    bit=(edge2*duv1[:,0,None]-edge1*duv2[:,0,None])*inv[:,None]
    total=np.zeros_like(positions);bt=np.zeros_like(positions)
    for i in range(3):
        np.add.at(total,triangles[:,i],t);np.add.at(bt,triangles[:,i],bit)
    total-=normals*(total*normals).sum(axis=1,keepdims=True)
    length=np.linalg.norm(total,axis=1)
    bad=length<1e-10
    if bad.any():
        axis=np.tile([1.,0.,0.],(bad.sum(),1));n=normals[bad]
        axis[np.abs(n[:,0])>.9]=[0.,1.,0.]
        total[bad]=np.cross(axis,n)
    total/=np.maximum(np.linalg.norm(total,axis=1,keepdims=True),1e-10)
    sign=np.where((np.cross(normals,total)*bt).sum(axis=1)<0,-1.,1.)
    return np.column_stack([total,sign])


def tail_partition(triangles, joints, weights, tail_ids, hips_id):
    """Keep the body/tail seam on the same animated bones on both meshes."""
    influence=(weights*np.isin(joints,tail_ids)).sum(axis=1)
    selected=influence[triangles].mean(axis=1)>.025
    boundary=np.intersect1d(np.unique(triangles[selected]),np.unique(triangles[~selected]))
    result=joints.copy()
    if len(boundary):
        values=result[boundary].copy()
        values[np.isin(values,tail_ids)]=hips_id
        result[boundary]=values
    return selected,result,int(len(boundary))


def part_skin_names(part,target_names,tail_names,primitives):
    if part!='tail':return [n for n in target_names if n not in tail_names]
    used={int(j) for p in primitives for j in p['joints'][p['weights']>1e-7]}
    return [n for i,n in enumerate(target_names) if i in used or n=='Hips' or n in tail_names]


def stitch_tail_seams(pieces,target_names,tail_names):
    """UV duplicates on the body/tail split must share one skin binding."""
    groups={}
    for part,primitives in pieces.items():
        for primitive in primitives:
            for vertex,position in enumerate(primitive['positions']):
                key=(primitive['material'],*np.round(position,5))
                groups.setdefault(key,[]).append((part,primitive,vertex))
    tail_ids={target_names.index(n) for n in tail_names}
    hips=target_names.index('Hips');stitched=0
    for rows in groups.values():
        parts={row[0] for row in rows}
        if not {'body','tail'}.issubset(parts):continue
        body=[row for row in rows if row[0]=='body'];dense=np.zeros(len(target_names))
        position=np.mean([p['positions'][i] for _,p,i in body],axis=0)
        for _,p,i in body:
            for joint,weight in zip(p['joints'][i],p['weights'][i]):
                dense[hips if int(joint) in tail_ids else int(joint)]+=float(weight)/len(body)
        selected=np.argsort(-dense)[:4];weights=dense[selected];weights/=max(weights.sum(),1e-10)
        for _,p,i in rows:
            p['joints'][i]=selected;p['weights'][i]=weights;p['positions'][i]=position
            stitched+=1
    return stitched


def retarget(source_path, target_rig, directory):
    doc, binary = load_glb(source_path)
    if len(doc.get("skins",[]))!=1:
        raise ValueError("Per sostituire V serve una base con uno scheletro umanoide. Questo file è disponibile solo per anteprima.")
    world = world_matrices(doc)
    skin = doc["skins"][0]
    source_names = [doc["nodes"][j]["name"] for j in skin["joints"]]
    required={"Hips","Neck","Head"}
    if not required.issubset(source_names):
        raise ValueError("Mapping dello scheletro incompleto: "+", ".join(sorted(required-set(source_names))))
    import physics
    source_tail=physics.tail_chain(source_names)
    if len(source_tail)==1:
        raise ValueError("La coda ha un solo osso riconosciuto: serve il mapping di almeno due ossa per generare la catena fisica.")
    aliases = [f"dyng_avatar_tail_{source_tail.index(n):02}" if n in source_tail else bone_alias(n) for n in source_names]
    targets = rig_matrices(target_rig)
    targets={key:b.node_matrix(trs(matrix)) for key,matrix in targets.items()}
    missing = {n for n in aliases if not n.startswith("dyng_avatar_tail_")} - set(targets)
    if missing:
        raise ValueError("Il rig di V non contiene: " + ", ".join(sorted(missing)))
    target_names = list(dict.fromkeys(aliases))
    target_ids = {n: i for i, n in enumerate(target_names)}
    mesh_node = next(i for i, n in enumerate(doc["nodes"]) if "mesh" in n)
    mesh_world = world[mesh_node]
    ibms = b.accessor_array(doc, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
    source_world = mesh_world @ np.linalg.inv(ibms)
    # VRChat's model and Cyberpunk's exported character face opposite directions.
    turn = np.diag([-1., 1., -1., 1.])
    source_world = turn @ source_world
    mesh_world = turn @ mesh_world
    src = {n: i for i, n in enumerate(source_names)}
    positions = {n: source_world[i, :3, 3] for i, n in enumerate(source_names)}
    scale = np.linalg.norm(targets["Neck"][:3, 3]-targets["Hips"][:3, 3]) / np.linalg.norm(positions["Neck"]-positions["Hips"])
    child_names = {"Hips": "Spine", "Spine": "Chest", "Chest": "Neck", "Neck": "Head"}
    for side in ["Left", "Right"]:
        child_names.update({side+" shoulder": side+" arm", side+" arm": side+" elbow", side+" elbow": side+" wrist",
                            side+" wrist": "MiddleFinger1_"+side[0], side+" leg": side+" knee",
                            side+" knee": side+" ankle", side+" ankle": side+" toe"})
        for finger in ["Thumb", "Index", "Middle", "Ring", "Pinky"]:
            for j in [1, 2]:
                child_names[finger+f"Finger{j}_"+side[0]] = finger+f"Finger{j+1}_"+side[0]
    transforms = []
    for i, n in enumerate(source_names):
        if n in source_tail:
            transforms.append(np.eye(4));continue
        child = child_names.get(n)
        linear = np.eye(3) * scale
        if child in src and aliases[src[child]] != aliases[i]:
            delta_src = positions[child]-positions[n]
            delta_dst = targets[aliases[src[child]]][:3, 3]-targets[aliases[i]][:3, 3]
            if np.linalg.norm(delta_src) > 1e-6 and np.linalg.norm(delta_dst) > 1e-6:
                length_scale = np.linalg.norm(delta_dst) / np.linalg.norm(delta_src)
                linear = frame(delta_dst) @ np.diag([scale, scale, length_scale]) @ frame(delta_src).T
        matrix = np.eye(4); matrix[:3, :3] = linear
        matrix[:3, 3] = targets[aliases[i]][:3, 3] - linear @ positions[n]
        transforms.append(matrix)
    transforms = np.array(transforms)
    tail_names=physics.add_targets(source_names,source_world,transforms,targets,aliases)
    # Eye/jaw joints are merged into Head for this lightweight rig. Their
    # vertices must keep the Head's deformation, including their rest offsets;
    # snapping each joint origin onto Head collapses the eyes into the skull
    # and pulls the lower jaw open.
    for i,n in enumerate(source_names):
        if aliases[i] == "Head" and n != "Head":
            transforms[i] = transforms[src["Head"]]
        elif n == "UpperChest" and "Chest" in src:
            transforms[i] = transforms[src["Chest"]]
    material_info = doc.get("materials", [])
    from geometry import accessory_weights,dorsal_crest_triangles,body_regions,fit_dorsal_shells,fit_pelvis_profile
    accessories,attachment_info=accessory_weights(doc,binary)
    dorsal_fits=[]
    pelvis_fits=[]
    removed_crest=0
    pieces = {"body": [], "head": [], "body_fpp": [], "arms_fpp": []}
    if tail_names:pieces["tail"]=[]
    neck_height = positions["Neck"][1] - .015
    for mesh in doc["meshes"]:
        for prim in mesh["primitives"]:
            mat_id = prim.get("material", 0)
            mat = material_info[mat_id]
            if "eyelens" in mat.get("name", "").lower():
                continue  # transparent specular overlay; the actual iris is a separate original submesh.
            attrs = prim["attributes"]
            p = b.accessor_array(doc, binary, attrs["POSITION"])
            n = b.accessor_array(doc, binary, attrs["NORMAL"]) if "NORMAL" in attrs else np.tile([0.,0.,1.], (len(p),1))
            uv = b.accessor_array(doc, binary, attrs["TEXCOORD_0"])
            joints = b.accessor_array(doc, binary, attrs["JOINTS_0"]).astype(np.int32)
            weights = b.accessor_array(doc, binary, attrs["WEIGHTS_0"]).astype(np.float32)
            if id(prim) in accessories:
                joints,weights=accessories[id(prim)]
            p = p @ mesh_world[:3,:3].T + mesh_world[:3,3]
            n = n @ np.linalg.inv(mesh_world[:3,:3])
            blend = (transforms[joints] * weights[:,:,None,None]).sum(axis=1)
            facial={}
            from facial import canonical
            for target_name,target in zip(mesh.get('extras',{}).get('targetNames',[]),prim.get('targets',[])):
                key=canonical(target_name)
                if key and 'POSITION' in target:
                    delta=b.accessor_array(doc,binary,target['POSITION'])@mesh_world[:3,:3].T
                    facial[key]=np.einsum('nij,nj->ni',blend[:,:3,:3],delta)
            result_pos = np.einsum("nij,nj->ni", blend[:,:3,:3], p) + blend[:,:3,3]
            result_norm = np.einsum("nij,nj->ni", np.linalg.inv(blend[:,:3,:3]).transpose(0,2,1), n)
            result_norm /= np.maximum(np.linalg.norm(result_norm,axis=1,keepdims=True),1e-8)
            mapped = np.array([target_ids[x] for x in aliases],dtype=np.uint16)[joints]
            tri = b.accessor_array(doc,binary,prim["indices"]).reshape(-1,3).astype(np.uint32)
            if any(word in mat.get('name','').lower() for word in ['body','skin']):
                tail_influence=(weights*np.isin(mapped,[target_ids[n] for n in tail_names])).sum(1)
                tail_vertices=np.unique(tri[tail_influence[tri].mean(1)>.025])
                result_pos,result_norm,mapped,weights,dorsal_fit=fit_dorsal_shells(result_pos,result_norm,uv,tri,mapped,weights,targets,target_names,tail_vertices)
                dorsal_fits.append({'material':mat.get('name',''),**dorsal_fit})
                hip_weights=(weights*(mapped==target_ids['Hips'])).sum(axis=1)
                result_pos,result_norm,pelvis_fit=fit_pelvis_profile(result_pos,result_norm,hip_weights,targets)
                pelvis_fits.append({'material':mat.get('name',''),**pelvis_fit})
            if any(word in mat.get('name','').lower() for word in ['feather','fluff','fur','crest','mane']) and not any(word in mat.get('name','').lower() for word in ['body','skin','eye']):
                keep=dorsal_crest_triangles(p,tri,positions)
                removed_crest+=int((~keep).sum());tri=tri[keep]
            is_head = (p[tri,1].mean(axis=1) >= neck_height)
            if tail_names:
                is_tail,mapped,_=tail_partition(tri,mapped,weights,[target_ids[n] for n in tail_names],target_ids['Hips'])
            else:is_tail=np.zeros(len(tri),dtype=bool)
            arm_ids=[target_ids[name] for name in target_names if name.startswith(('Left','Right')) and any(s in name for s in ['Arm','Hand','Shoulder'])]
            arm_weight=(weights*np.isin(mapped,arm_ids)).sum(axis=1)
            is_arm=arm_weight[tri].mean(axis=1)>.45
            selections=[("body",tri[~is_head & ~is_tail]),("head",tri[is_head & ~is_tail])]
            selections.extend([('body_fpp',tri[~is_head & ~is_tail & ~is_arm]),('arms_fpp',tri[~is_head & ~is_tail & is_arm])])
            if tail_names:selections.append(("tail",tri[is_tail]))
            for part, selected in selections:
                if not len(selected):
                    continue
                chunks=[(selected,'keep')]
                if part in ['body','body_fpp','arms_fpp']:
                    hip_weights=(weights*(mapped==target_ids['Hips'])).sum(axis=1)
                    regions=body_regions(result_pos,selected,targets,arm_weight,hip_weights)
                    chunks=[(selected[regions==region],str(region)) for region in np.unique(regions)]
                for subset,region in chunks:
                    if not len(subset):continue
                    used, remapped = np.unique(subset.reshape(-1),return_inverse=True)
                    pieces[part].append({"positions": result_pos[used], "normals": result_norm[used], "uv": uv[used],
                                         "joints": mapped[used], "weights": weights[used],
                                         "indices": remapped.astype(np.uint32), "material": mat_id,'covered_pelvis':region=='pelvis','region':region,
                                         "facial":{key:delta[used] for key,delta in facial.items()} if part=='head' else {}})
    attachment_info['stitched_tail_boundary_vertices']=stitch_tail_seams(pieces,target_names,tail_names) if tail_names else 0
    paths = {}
    for part, primitives in pieces.items():
        if not primitives:continue
        out = b.GLB()
        out.doc['extras']={'avatarbridge_intact_skin':1}
        part_names=part_skin_names(part,target_names,tail_names,primitives)
        joint_map=np.array([part_names.index(n) if n in part_names else part_names.index("Hips") for n in target_names],dtype=np.uint16)
        for target in part_names:
            out.doc["nodes"].append({"name": target, **trs(targets[target])})
        inverse = np.array([np.linalg.inv(targets[n]).T.reshape(-1) for n in part_names])
        out.doc["skins"] = [{"joints": list(range(len(part_names))), "inverseBindMatrices": out.array(inverse,"MAT4")}]
        for i, primitive in enumerate(primitives):
            label = f"submesh_{i:02}_LOD_1"
            attributes = {"POSITION": out.array(primitive["positions"],"VEC3",bounds=True,target=34962),
                          "NORMAL": out.array(primitive["normals"],"VEC3",target=34962),
                          "TANGENT": out.array(tangents(primitive["positions"],primitive["normals"],primitive["uv"],primitive["indices"]),"VEC4",target=34962),
                          "TEXCOORD_0": out.array(primitive["uv"],"VEC2",target=34962),
                          "JOINTS_0": out.array(joint_map[primitive["joints"]],"VEC4",5123,target=34962),
                          "WEIGHTS_0": out.array(primitive["weights"],"VEC4",target=34962)}
            out.doc["materials"].append(copy.deepcopy(material_info[primitive["material"]]))
            out.doc["materials"][-1]["pbrMetallicRoughness"].pop("baseColorTexture",None)
            out.doc["meshes"].append({"name": label,"extras":{'avatarbridge_region':primitive.get('region','keep'),'avatarbridge_coverage':'pelvis' if primitive.get('covered_pelvis') else ''},"primitives":[{"attributes":attributes,"mode":4,
                                      "material":i,"indices":out.array(primitive["indices"],"SCALAR",5125,target=34963)}]})
            out.doc["nodes"].append({"name":label,"mesh":i,"skin":0})
        all_nodes = list(range(len(out.doc["nodes"])))
        out.doc["nodes"].append({"name":"AvatarBridge","children":all_nodes})
        out.doc["scenes"][0]["nodes"] = [len(out.doc["nodes"])-1]
        path = Path(directory) / (part+".glb"); out.save(path); paths[part] = path
    attachment_info['removed_dorsal_crest_triangles']=removed_crest
    attachment_info['dorsal_shell_fits']=dorsal_fits
    if any(len(pieces[part])>52 for part in ['body','body_fpp','arms_fpp']):
        raise ValueError('Troppe sezioni del corpo per una maschera Lua esatta')
    coverage={part:{'full':(1<<len(values))-1,'clothed':sum(1<<i for i,p in enumerate(values) if not p.get('covered_pelvis')),
                    'zones':[{'bit':1<<i,'region':p.get('region','keep')} for i,p in enumerate(values)]}
              for part,values in pieces.items() if part in ['body','body_fpp','arms_fpp']}
    skin_materials={i for i,mat in enumerate(material_info) if any(word in mat.get('name','').lower() for word in ['body','skin']) and 'eyelens' not in mat.get('name','').lower()}
    source_skin=sum(doc['accessors'][p['indices']]['count']//3 for mesh in doc['meshes'] for p in mesh['primitives'] if p.get('material',0) in skin_materials)
    target_skin=sum(len(p['indices'])//3 for part in ['body','head','tail'] for p in pieces.get(part,[]) if p['material'] in skin_materials)
    if source_skin!=target_skin:raise ValueError('Il retarget ha perso triangoli della pelle')
    attachment_info['pelvis_shell_fits']=pelvis_fits
    return paths, pieces, target_names, material_info, {"mapping":dict(zip(source_names,aliases)),"radial_scale":float(scale),"attachments":attachment_info,
        'coverage':coverage,'skin_integrity':{'source_skin_triangles':source_skin,'retargeted_skin_triangles':target_skin,'skin_triangles_removed':0},
        "_tail_names":tail_names,"_tail_targets":{n:m.tolist() for n,m in targets.items()} if tail_names else {}}


def make_mesh(template, glb, primitives, material_info, textures, output_dir, file_name):
    doc = copy.deepcopy(template)
    root = doc["Data"]["RootChunk"]
    mats = []
    for p in primitives:
        i = p["material"]
        mat_name = material_info[i].get("name", "")
        eye = "eye" in mat_name.lower()
        values = [{"$type":"rRef:ITexture", "BaseColor":ref(textures[i])},
                  {"$type":"Float", "MetalnessScale":0}, {"$type":"Float", "MetalnessBias":0},
                  {"$type":"Float", "RoughnessScale":0}, {"$type":"Float", "RoughnessBias":.3 if eye else .85},
                  {"$type":"Float", "AlphaThreshold":.5}]
        mats.append({"$type":"CMaterialInstance","audioTag":name("None"),
                     "baseMaterial":ref("engine\\materials\\metal_base.remt"), "enableMask":1 if "feather" in mat_name.lower() else 0,
                     "resourceVersion":4,"cookingPlatform":"PLATFORM_PC","values":values})
    count = len(mats)
    root["materialEntries"] = [{"$type":"CMeshMaterialEntry","index":i,"isLocalInstance":1,"name":name(f"avatar_{i}")} for i in range(count)]
    root["localMaterialBuffer"] = {"$type":"meshMeshMaterialBuffer","materials":mats}
    for key in ["externalMaterials","localMaterialInstances","preloadExternalMaterials","preloadLocalMaterialInstances","parameters"]:
        root[key] = []
    root["appearances"] = [{"HandleId":"0","Data":{"$type":"meshMeshAppearance","name":name("default"),
                           "chunkMaterials":[name(f"avatar_{i}") for i in range(count)],"tags":[]}}]
    # Start a new orphan mesh skeleton rather than carrying the vanilla muscle rig into the mod.
    for key in ["boneNames","boneRigMatrices","boneVertexEpsilons","lodBoneMask"]:
        root[key] = []
    blob = root.get("renderResourceBlob",{}).get("Data",{})
    if "header" in blob:
        blob["header"]["bonePositions"] = []
    mesh = deserialize(doc,file_name,output_dir)
    shutil.copy2(glb, Path(output_dir) / (Path(file_name).stem+".glb"))
    before = hashlib.sha256(mesh.read_bytes()).hexdigest()
    command("import",Path(output_dir)/(Path(file_name).stem+".glb"),"-o",output_dir,"--keep")
    if hashlib.sha256(mesh.read_bytes()).hexdigest() == before:
        raise ValueError("La geometria non è stata importata")
    return mesh


def patch_ent(template, components, filename, dest):
    doc = copy.deepcopy(template)
    r = doc["Data"]["RootChunk"]
    components=copy.deepcopy(components)
    # Each body's skinning binding must remain distinct from its tail rig.
    # Reusing a JSON HandleId aliases the bindings during deserialization.
    for i,c in enumerate(components):
        for j,key in enumerate(["parentTransform","skinning","controlBinding"]):
            if c.get(key) and "Data" in c[key]:c[key]["HandleId"]=str(10000+i*3+j)
    # Replace the native surfaces as well as adding the new skinned body. This
    # also applies to inventory/photo-mode entities, where a player-only CET
    # Toggle cannot reach the rendered paperdoll.
    hidden = [disabled_component(c) for c in r.get("components", [])
              if original_surface(c)]
    r["components"] = hidden + components
    r["entity"] = None
    r["compiledData"] = None
    for key in ["appearances","backendDataOverrides","bindingOverrides","componentResolveSettings","includes","inplaceResources","resolvedDependencies"]:
        r[key] = []
    r["localData"] = None
    return deserialize(doc,filename,dest)


def original_surface(c):
    n = c.get("name", {}).get("$value", "")
    return "MeshComponent" in c.get("$type", "") and (
        n in {"body", "torso", "legs", "shoes", "head_shadowmesh"} or
        re.match(r"^(t0_000_|a0_00[01]_|n0_000_|MorphTargetSkinnedMesh|h[0ehbtx]_0|heb_|hh_|ha_|hb_)", n))


def disabled_component(c):
    # No reference to the old mesh/morph resource: appearance controllers may
    # re-enable a component, but they must never bring the human body back.
    return {"$type": c["$type"], "id": c["id"], "name": copy.deepcopy(c["name"]),
            "isEnabled": 0, "chunkMask": "0", "castShadows": "Never",
            "castLocalShadows": "Never"}


def surface_patch(template, code):
    doc = copy.deepcopy(template)
    root = doc["Data"]["RootChunk"]
    definitions = []
    for item in root.get("appearances", []):
        a = item["Data"]
        appearance = a["name"]["$value"]
        components = a.get("components", [])
        # Some common appearances have no gender in their name. Resource
        # paths and component names still identify which player's skin it is.
        selected = code in appearance or code in json.dumps(components)
        if not selected:
            continue
        hidden = [disabled_component(c) for c in components
                  if "MeshComponent" in c.get("$type", "")]
        if not hidden:
            continue
        definitions.append({"HandleId": str(len(definitions)), "Data": {
            "$type": "appearanceAppearanceDefinition", "name": copy.deepcopy(a["name"]),
            "components": hidden, "compiledData": None}})
    root["appearances"] = definitions
    for key in ["alternateAppearanceMapping", "alternateAppearanceSuffixes", "censorshipMapping"]:
        root[key] = []
    root["commonCookData"] = None
    return doc if definitions else None


def native_surface_patches(game, code, dest):
    refs = b.DATA / "build-reference/surfaces"
    refs.mkdir(parents=True, exist_ok=True)
    # Only the player's skin/face resources; clothing appearances stay intact.
    pattern = r"(player_base_bodies\\appearances\\(t0_000_base__full|arms\\a0_000_fpp__full)\.app$|player_base_heads\\appearances\\(head|eyes|teeth|eyebrows|hairs|beards)\\.*\.app$)"
    command("unbundle", Path(game)/"archive/pc/content/basegame_4_appearance.archive", "-o", refs, "-r", pattern)
    # Run conversion once per directory, rather than starting WolvenKit for
    # each of hundreds of appearance resources.
    sources = sorted(refs.rglob("*.app"))
    from collections import Counter
    counts = Counter(p.name for p in sources)
    if any(not (refs/(p.name+".json")).is_file() for p in sources):
        command("convert", "serialize", refs, "-o", refs, "-w", "*.app")
    patches = {}
    for source in sources:
        # Cache the public-game reference's JSON locally, never distribute it.
        cached = source.with_suffix(".app.json") if counts[source.name] > 1 else refs/(source.name+".json")
        template = json.loads(cached.read_text(encoding="utf-8-sig")) if cached.is_file() else serialize(source, source.parent)
        patched = surface_patch(template, code)
        if patched is None:
            continue
        filename = "surface_" + hashlib.sha256(str(source.relative_to(refs)).encode()).hexdigest()[:12] + ".app"
        (Path(dest)/(filename+".json")).write_text(json.dumps(patched), encoding="utf-8")
        patches[PREFIX + "\\" + filename] = [str(source.relative_to(refs))]
    if len(patches) < 3:
        raise ValueError("Risorse del corpo originale incomplete: installazione interrotta")
    command("convert", "deserialize", dest, "-o", dest, "-r", r"surface_.*\.app\.json$")
    if not all((Path(dest)/Path(key).name).is_file() for key in patches):
        raise ValueError("Patch delle superfici non compilate")
    return patches


def component(part, index):
    return {"$type":"entSkinnedMeshComponent","id":str(1800000000000000000+index),
            "name":name("avatarbridge_"+part),"isEnabled":1,"isReplicable":0,
            "mesh":ref(PREFIX+"\\"+part+".mesh"),"meshAppearance":name("default"),
            "chunkMask":"18446744073709551615","castShadows":"Always","castLocalShadows":"Always",
            "renderSceneLayerMask":"Default","autoHideDistance":0,
            "parentTransform":{"HandleId":str(100+index*2),"Data":{"$type":"entHardTransformBinding","bindName":name("root"),"enabled":1,"slotName":name("None")}},
            "skinning":{"HandleId":str(101+index*2),"Data":{"$type":"entSkinningBinding","bindName":name("root"),"enabled":1}}}


LUA = '''local time = 0
local attached = false
local warned = false
local function original(name)
  if name:find("avatarbridge_", 1, true) then return false end
  return name == "body" or name == "head_shadowmesh" or name:find("MorphTargetSkinnedMesh",1,true)
    or name:match("^t0_000_p[wm]a_") or name:match("^a0_00[01]_p[wm]a_")
    or name:match("^h[0ehbtx]_0") or name:match("^heb_") or name:match("^hh_")
    or name:match("^ha_") or name:match("^hb_")
end
registerForEvent("onInit", function() print("Avatar Bridge: local player replacement loaded") end)
registerForEvent("onUpdate", function(delta)
  time = time + delta
  if time < 0.25 then return end
  time = 0
  local player = Game.GetPlayer()
  if not player then return end
  local ready = player:FindComponentByName(CName.new("avatarbridge_body"))
  if not ready then
    if not warned then print("Avatar Bridge: waiting for player component"); warned = true end
    return
  end
  local hidden = 0
  for _, component in ipairs(player:GetComponents()) do
    local n = NameToString(component.name)
    if original(n) then
      local ok = pcall(function() component:Toggle(false) end)
      if ok then hidden = hidden + 1 end
    end
  end
  if not attached then print("Avatar Bridge: player body attached; original components hidden: " .. tostring(hidden)); attached = true end
end)
'''


def build(job_path, game_path):
    global PREFIX
    job = Path(job_path).resolve()
    if not job.is_relative_to((b.DATA/"jobs").resolve()):
        raise ValueError("Lavoro fuori dalla libreria locale")
    report = json.loads((job/"report.json").read_text(encoding="utf-8"))
    report = b.refresh_face_profile(report,job)
    report = b.refresh_tail_profile(report,job)
    PREFIX = "avatarbridge\\" + job.name
    game = Path(b.inspect_game(game_path)["game"])
    gender = report["gender"]
    code, short, rig_name = ("pwa","wa","woman") if gender == "female" else ("pma","ma","man")
    refs = b.DATA/"build-reference"/gender; refs.mkdir(parents=True,exist_ok=True)
    content = game/"archive/pc/content"
    command("unbundle",content/"basegame_4_appearance.archive","-o",refs,"-r",
            rf"(t0_000_{code}_base__full\.mesh$|player_{rig_name}_(base|fpp)_deformations\.rig$)")
    command("unbundle",content/"basegame_1_engine.archive","-o",refs,"-r",rf"player_{short}_fpp\.ent$")
    command("unbundle",content/"memoryresident_1_general.archive","-o",refs,"-w","*metal_base.remt")
    if not (refs/"engine/materials/metal_base.remt").is_file():
        raise ValueError("Shader di base non trovato nel gioco")
    body_ref = next(refs.rglob(f"t0_000_{code}_base__full.mesh"))
    rig_candidates=list(refs.rglob(f"player_{rig_name}_base_deformations.rig")) or list(refs.rglob(f"player_{rig_name}_fpp_deformations.rig"))
    if not rig_candidates:raise ValueError("Rig del corpo non trovato")
    rig_ref=rig_candidates[0]
    ent_ref = next(refs.rglob(f"player_{short}_fpp.ent"))
    mesh_template = serialize(body_ref,refs)
    rig_template = serialize(rig_ref,refs)
    ent_template = serialize(ent_ref,refs)
    working = job/("build_"+uuid.uuid4().hex[:8]); working.mkdir()
    archive = working/"archive"; archive.mkdir()
    raw = working/"raw"; raw.mkdir()
    if not b.glb_doc(report["output"]).get("skins"):
        raise ValueError("Il modello non contiene uno scheletro: puoi vederlo in anteprima, ma per usarlo come V serve la versione riggata.")
    adapted_source=raw/"body-profile.glb"
    b.reduce_rig(report["output"],adapted_source)
    parts, pieces, bones, materials, mapping = retarget(adapted_source,rig_template,raw)
    if not {'body_fpp','arms_fpp'}.issubset(parts):
        raise ValueError('La base deve contenere torso e braccia per generare il corpo giocabile e la visuale in prima persona.')
    src_doc, src_binary = load_glb(report["output"])
    config_path = cli().parent/"appsettings.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    config["XbmImportArgs"] = {"IsGamma":True,"GenerateMipMaps":True,"Compression":"TCM_DXTAlpha"}
    config["GltfImportArgs"] = {"ImportGarmentSupport":False,"ValidationMode":"Strict","ImportMaterials":False}
    config["MeshExportArgs"] = {"withMaterials":False}
    config_path.write_text(json.dumps(config,indent=2),encoding="utf-8")
    texture_paths = {}
    texture_cache=b.DATA/"texture-cache";texture_cache.mkdir(exist_ok=True)
    for i in sorted({p["material"] for primitives in pieces.values() for p in primitives}):
        texture_id = materials[i]["pbrMetallicRoughness"].get("baseColorTexture",{}).get("index")
        if texture_id is None:
            raise ValueError("Una parte della base non ha la texture originale")
        image = src_doc["images"][src_doc["textures"][texture_id]["source"]]
        view = src_doc["bufferViews"][image["bufferView"]]
        filename = f"texture_{i}"+(".png" if image["mimeType"]=="image/png" else ".jpg")
        p = raw/filename
        original_bytes=src_binary[view.get("byteOffset",0):view.get("byteOffset",0)+view["byteLength"]]
        p.write_bytes(original_bytes)
        cached=texture_cache/(hashlib.sha256(original_bytes+b"2048-srgb-dxt5").hexdigest()+".xbm")
        from PIL import Image
        with Image.open(p) as original:
            image_copy = original.convert("RGBA")
        image_copy.thumbnail((2048,2048),Image.Resampling.LANCZOS)
        image_copy.save(p)
        tex_dir = archive/Path(PREFIX);tex_dir.mkdir(parents=True,exist_ok=True)
        out = tex_dir/(p.stem+".xbm")
        if cached.is_file():
            shutil.copy2(cached,out)
        else:
            # Import ready-compressed DDS rather than repeatedly recompressing PNG in WolvenKit.
            dds=p.with_suffix(".dds");image_copy.save(dds,pixel_format="DXT5")
            command("import",dds,"-o",tex_dir)
            if out.is_file() and out.stat().st_size>128:
                shutil.copy2(out,cached)
        if not out.is_file():
            raise ValueError("Texture del gioco non generata")
        texture_paths[i] = PREFIX+"\\"+out.name
    model_dir = archive/Path(PREFIX)
    validation = []
    for part,path in parts.items():
        mesh = make_mesh(mesh_template,path,pieces[part],materials,texture_paths,model_dir,part+".mesh")
        exported = working/("verify_"+part);exported.mkdir()
        command("export",mesh,"-o",exported)
        expected = sum(len(p["indices"])//3 for p in pieces[part])
        stats = b.geometry_stats(exported/(part+".glb"))
        expected_bones=len(b.glb_doc(path)["skins"][0]["joints"])
        if stats["triangles"] != expected or stats["bones"] < expected_bones:
            raise ValueError("Verifica del modello compilato non riuscita")
        validation.append({"part":part,"expected_triangles":expected,**stats})
    import physics
    tail_names=mapping.pop("_tail_names",[]);tail_targets=mapping.pop("_tail_targets",{})
    tail_doc=b.glb_doc(parts['tail']) if 'tail' in parts else None
    tail_bones=[tail_doc['nodes'][i]['name'] for i in tail_doc['skins'][0]['joints']] if tail_doc else []
    tail_info=physics.resources(rig_template,{n:np.array(m) for n,m in tail_targets.items()},tail_names,model_dir,tail_bones) if "tail" in parts else None
    extra=[]
    if tail_info:
        tail=component("tail",3);tail["skinning"]["Data"]["bindName"]=name("avatarbridge_tail_rig")
        extra=[physics.animated_component("avatarbridge_tail_rig",PREFIX,1800000000000000004),tail]
    fpp_body=component('body_fpp',1);fpp_body['name']=name('avatarbridge_body')
    fpp_arms=component('arms_fpp',5);fpp_arms['renderingPlaneAnimationParam']=name('renderPlane')
    fpp = patch_ent(ent_template,[fpp_body,fpp_arms]+extra,"player_fpp.ent",model_dir)
    tpp = patch_ent(ent_template,[component("body",1),component("head",2)]+extra,"player_tpp.ent",model_dir)
    for p in [fpp,tpp]:
        roundtrip = serialize(p,raw)
        if not roundtrip["Data"]["RootChunk"].get("components"):
            raise ValueError("La patch del giocatore non contiene componenti")
    surface_patches = native_surface_patches(game,code,model_dir)
    import garments
    fitted_garments = garments.build(game,code,parts,model_dir,PREFIX)
    import facial
    face_info=facial.build(mesh_template,parts['head'],pieces['head'],materials,texture_paths,model_dir,PREFIX)
    # Only cooked resources belong in the archive.
    for p in model_dir.iterdir():
        if p.suffix in {".glb",".json"}:
            p.unlink()
    package = working/"package";package.mkdir()
    archive_name = MOD+".archive"
    command("pack",archive,"-o",package)
    generated = list(package.glob("*.archive"))
    if len(generated)!=1:
        raise ValueError("Archivio mod non generato")
    final_archive = package/archive_name
    if generated[0] != final_archive:
        generated[0].rename(final_archive)
    base = "base\\characters\\entities\\player\\"
    patches = {PREFIX+"\\player_fpp.ent":[base+f"player_{short}_fpp.ent",f"ep1\\characters\\entities\\player\\player_{short}_fpp_ep1.ent"],
               PREFIX+"\\player_tpp.ent":[base+f"player_{short}_tpp.ent",base+f"player_{short}_tpp_cutscene.ent",
               base+f"player_{short}_tpp_cutscene_no_impostor.ent",base+f"player_{short}_tpp_reflexion.ent",
               base+f"photo_mode\\player_{short}_photomode.ent",f"ep1\\characters\\entities\\player\\player_{short}_tpp_ep1.ent",
               f"ep1\\characters\\entities\\player\\photo_mode\\player_{short}_photomode_ep1.ent"]}
    patches.update(surface_patches)
    import yaml
    (package/(archive_name+".xl")).write_text(yaml.safe_dump({"resource":{"patch":patches}},sort_keys=False),encoding="utf-8")
    (package/"init.lua").write_text(LUA,encoding="utf-8")
    info = {"stage":"compiled","builder_version":24,"tail_binding_version":1,"pelvis_profile_version":2,"intact_skin_version":1,"resource_prefix":PREFIX,"first_person":{"body":"body_fpp.mesh","arms":"arms_fpp.mesh"},"tail":tail_info,"face":face_info,'coverage':mapping.get('coverage',{}),'coverage_algorithm':4,'skin_integrity':mapping.get('skin_integrity',{}),"hide_footwear":True,"garments":fitted_garments,"garment_refit_version":11,"garment_seam_version":1,"garment_layer_version":2,"dorsal_fit_version":3,"runtime_verified":False,"gender":gender,"package":str(package),
            "archive":str(final_archive),"sha256":hashlib.sha256(final_archive.read_bytes()).hexdigest(),
            "parts":validation,"mapping":mapping,"eyes":"Original avatar iris geometry and red albedo; transparent lens overlay omitted.",
            "limitations":["Expressions use original avatar shapes when available, with automatic blinking; dialogue lip sync is not implemented.",
                           "The garment catalog covers matching base-game player meshes; custom outfits need separate fitting.","In-game verification is recorded separately from successful compilation."]}
    (working/"build.json").write_text(json.dumps(info,indent=2),encoding="utf-8")
    (job/"build.json").write_text(json.dumps(info,indent=2),encoding="utf-8")
    return info


def running_game(game_path=None):
    import subprocess
    if game_path is not None:
        # An isolated test copy has its own executable and mod files. Only a
        # process using the target installation prevents its modification.
        query = ('[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; '
                 '@(Get-CimInstance Win32_Process -Filter "Name=\'Cyberpunk2077.exe\'" '
                 '-ErrorAction Stop | Select-Object -ExpandProperty ExecutablePath) | ConvertTo-Json -Compress')
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            try:
                paths = json.loads(result.stdout.decode("utf-8-sig"))
                paths = paths if isinstance(paths, list) else [paths]
                expected = (Path(game_path) / "bin/x64/Cyberpunk2077.exe").resolve()
                return any(not path or Path(path).resolve() == expected for path in paths)
            except (ValueError, TypeError):
                pass
    out = subprocess.run(["tasklist","/FI","IMAGENAME eq Cyberpunk2077.exe","/FO","CSV","/NH"],capture_output=True).stdout
    return b"cyberpunk2077.exe" in out.lower()


def install(job_path, game_path):
    game = Path(b.inspect_game(game_path)["game"])
    if running_game(game):
        raise ValueError("Chiudi Cyberpunk prima di installare o rimuovere la mod")
    runtime = b.require_runtime(game)
    job = Path(job_path).resolve()
    if not job.is_relative_to((b.DATA/"jobs").resolve()):
        raise ValueError("Lavoro fuori dalla libreria locale")
    info = json.loads((job/"build.json").read_text())
    if runtime["mode"] == "compat25":
        import compat25
        info = compat25.build(job, game)
    elif info.get("builder_version",0)>=4:
        import library
        info = library.build(job,game)
    package = Path(info["package"]).resolve()
    if not package.is_relative_to(job):
        raise ValueError("Pacchetto fuori dalla libreria")
    sources = {f"archive/pc/mod/{MOD}.archive":package/(MOD+".archive"),
               f"archive/pc/mod/{MOD}.archive.xl":package/(MOD+".archive.xl"),
               "bin/x64/plugins/cyber_engine_tweaks/mods/AvatarBridge/init.lua":package/"init.lua"}
    if runtime["mode"] == "compat25":
        sources["bin/x64/plugins/AvatarBridge25.asi"] = package/"AvatarBridge25.asi"
    for p in sources.values():
        if not p.is_file():
            raise ValueError("Pacchetto incompleto")
    if hashlib.sha256(sources[f"archive/pc/mod/{MOD}.archive"].read_bytes()).hexdigest()!=info["sha256"]:
        raise ValueError("Archivio modificato dopo la verifica")
    state = b.DATA/"installations";state.mkdir(parents=True,exist_ok=True)
    key = hashlib.sha256(str(game).lower().encode()).hexdigest()[:16]
    record_path = state/(key+".json")
    if record_path.is_file():
        uninstall(game_path)
    backup = state/(key+"_"+uuid.uuid4().hex[:8]);backup.mkdir()
    records=[];temporary=[]
    try:
        disabled = [(game/"archive/pc/mod/ExoticsCO-ExoticV.archive", "disabled_conflict")]
        if runtime["mode"] == "compat25":
            disabled.append((game/"engine/config/base/scripts.ini", "disabled_dependency"))
        for conflict, reason in disabled:
            if conflict.is_file():
                original=backup/conflict.name;shutil.copy2(conflict,original)
                records.append({"path":str(conflict),"backup":str(original),"sha256":None,"mode":reason})
                conflict.unlink()
        for i,(relative,src) in enumerate(sources.items()):
            dst=(game/relative).resolve()
            if not dst.is_relative_to(game):
                raise ValueError("Destinazione fuori dal gioco")
            original=None
            if dst.exists():
                original=backup/str(i);shutil.copy2(dst,original)
            dst.parent.mkdir(parents=True,exist_ok=True)
            row={"path":str(dst),"backup":str(original) if original else None,"sha256":hashlib.sha256(src.read_bytes()).hexdigest()}
            records.append(row)
            tmp=dst.with_name(dst.name+".avatarbridge-tmp")
            temporary.append(tmp)
            shutil.copy2(src,tmp);os.replace(tmp,dst)
        record={"game":str(game),"job":str(job),"files":records,"time":time.time(),
                "mode":runtime["mode"],"runtime_verified":False}
        record_path.write_text(json.dumps(record,indent=2),encoding="utf-8")
    except Exception:
        for row in reversed(records):
            dst=Path(row["path"])
            if row["backup"]:shutil.copy2(row["backup"],dst)
            elif dst.exists():dst.unlink()
        for tmp in temporary:
            if tmp.exists():tmp.unlink()
        raise
    return {"stage":"installed","files":len(records),"game":str(game),
            "mode":runtime["mode"],"runtime_verified":False}


def uninstall(game_path):
    game=Path(b.inspect_game(game_path)["game"])
    if running_game(game):raise ValueError("Chiudi Cyberpunk prima di rimuovere la mod")
    key=hashlib.sha256(str(game).lower().encode()).hexdigest()[:16]
    record_path=b.DATA/"installations"/(key+".json")
    if not record_path.is_file():return {"stage":"not_installed"}
    record=json.loads(record_path.read_text())
    for row in record["files"]:
        p=Path(row["path"]).resolve()
        if not p.is_relative_to(game):raise ValueError("Manifest installazione non valido")
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()!=row["sha256"]:
            raise ValueError("Un file della mod è stato modificato: ripristino automatico interrotto")
    for row in reversed(record["files"]):
        p=Path(row["path"])
        if row["backup"]:shutil.copy2(row["backup"],p)
        elif p.exists():p.unlink()
    record_path.unlink()
    return {"stage":"removed"}
