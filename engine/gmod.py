"""Local Source playermodel compiler. Avatar and game resources stay on this PC."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zlib
import numpy as np
from PIL import Image
from scipy.spatial.transform import Rotation
import bridge as b

VERSION = 4


def game_root(path):
    root = Path(path).resolve()
    if root.name.lower() == 'garrysmod' and (root / 'gameinfo.txt').is_file():
        root = root.parent
    if not (root / 'garrysmod/gameinfo.txt').is_file():
        raise ValueError("Seleziona la cartella GarrysMod che contiene garrysmod/gameinfo.txt")
    return root


def compiler(root):
    for relative in ['bin/win64/studiomdl.exe', 'bin/studiomdl.exe']:
        if (root / relative).is_file():
            return root / relative
    raise ValueError('StudioMDL manca: verifica i file di Garry\'s Mod in Steam.')


def inspect_game(path):
    root = game_root(path)
    try:
        tool = str(compiler(root)); ready = True
    except ValueError:
        tool = None; ready = False
    return {'game': str(root), 'compiler': tool, 'runtime': {'can_install': ready,
        'summary': "Garry's Mod pronto: conversione locale in playermodel ValveBiped." if ready else 'StudioMDL mancante.'}}


def vpk_read(directory, wanted):
    """Read one local VPK entry, verifying its CRC; never extract the whole game."""
    directory = Path(directory)
    with directory.open('rb') as stream:
        signature, version, size = struct.unpack('<III', stream.read(12))
        if signature != 0x55AA1234 or version not in (1, 2):
            raise ValueError('VPK non supportato')
        header = 12 if version == 1 else 28
        stream.seek(header); tree = stream.read(size)
    cursor = 0
    def string():
        nonlocal cursor
        end = tree.index(b'\0', cursor)
        value = tree[cursor:end].decode('utf-8'); cursor = end + 1
        return value
    while ext := string():
        while folder := string():
            while filename := string():
                crc, preload, archive, offset, length, terminator = struct.unpack_from('<IHHIIH', tree, cursor)
                cursor += 18
                prefix = tree[cursor:cursor+preload]; cursor += preload
                name = (('' if folder == ' ' else folder + '/') + filename + '.' + ext).lower()
                if name != wanted.lower():
                    continue
                if terminator != 0xFFFF:
                    raise ValueError('Voce VPK non valida')
                datafile = directory if archive == 0x7FFF else directory.with_name(directory.name.replace('_dir.vpk', f'_{archive:03}.vpk'))
                with datafile.open('rb') as stream:
                    stream.seek(offset + (header + size if archive == 0x7FFF else 0))
                    result = prefix + stream.read(length)
                if zlib.crc32(result) & 0xFFFFFFFF != crc:
                    raise ValueError('File del gioco danneggiato: ' + wanted)
                return result
    raise FileNotFoundError(wanted)


def reference_bones(root):
    wanted = 'models/player/group01/male_07.mdl'
    loose = root / 'garrysmod' / wanted
    raw = loose.read_bytes() if loose.is_file() else vpk_read(root / 'garrysmod/garrysmod_dir.vpk', wanted)
    if raw[:4] != b'IDST':
        raise ValueError('Modello di riferimento Source non valido')
    count, start = struct.unpack_from('<II', raw, 156)
    if not 20 <= count <= 256:
        raise ValueError('Scheletro Source non valido')
    bones = []
    for i in range(count):
        offset = start + i * 216
        nameoffset, parent = struct.unpack_from('<ii', raw, offset)
        name = raw[offset+nameoffset:raw.index(b'\0', offset+nameoffset)].decode('ascii')
        position = np.array(struct.unpack_from('<3f', raw, offset+32))
        quaternion = struct.unpack_from('<4f', raw, offset+44)
        local = np.eye(4); local[:3,:3] = Rotation.from_quat(quaternion).as_matrix(); local[:3,3] = position
        world = (bones[parent]['world'] if parent >= 0 else np.eye(4)) @ local
        bones.append({'name': name, 'parent': parent, 'local': local, 'world': world})
    return bones


def bone_alias(value):
    key = re.sub('[^a-z0-9]', '', value.lower()).removeprefix('mixamorig')
    if key.startswith('valvebiped'):
        return value
    direct = {'hips':'Pelvis', 'pelvis':'Pelvis', 'spine':'Spine', 'chest':'Spine2',
              'upperchest':'Spine4', 'neck':'Neck1', 'head':'Head1'}
    if key in direct:
        return 'ValveBiped.Bip01_' + direct[key]
    for side, letter in [('left','L'), ('right','R')]:
        for old, new in {'shoulder':'Clavicle', 'arm':'UpperArm', 'upperarm':'UpperArm',
                'elbow':'Forearm', 'forearm':'Forearm', 'lowerarm':'Forearm', 'wrist':'Hand', 'hand':'Hand',
                'leg':'Thigh', 'upleg':'Thigh', 'upperleg':'Thigh', 'knee':'Calf', 'lowerleg':'Calf',
                'ankle':'Foot', 'foot':'Foot', 'toe':'Toe0', 'toes':'Toe0'}.items():
            if key == side + old:
                return f'ValveBiped.Bip01_{letter}_{new}'
    match = re.fullmatch(r'(thumb|index|middle|ring|pinky)finger([123])([lr])', key)
    if match:
        finger, segment, side = match.groups()
        number = ['thumb','index','middle','ring','pinky'].index(finger)
        suffix = '' if segment == '1' else str(int(segment)-1)
        return f'ValveBiped.Bip01_{side.upper()}_Finger{number}{suffix}'
    return None


def frame(axis, forward):
    length = np.linalg.norm(axis)
    if length < 1e-5:
        raise ValueError('Due articolazioni coincidenti non definiscono una rotazione.')
    x = axis / length
    z = forward - x * (x @ forward)
    if np.linalg.norm(z) < 1e-5:
        fallback = np.eye(3)[np.argmin(np.abs(x))]
        z = fallback - x * (x @ fallback)
    z /= np.linalg.norm(z)
    return np.column_stack([x, np.cross(z, x), z])


def segment_transform(start, end, target_start, target_end, forward):
    """Map both joint endpoints, retaining the transverse mesh thickness."""
    source_axis=end-start; target_axis=target_end-target_start
    source_length=np.linalg.norm(source_axis); target_length=np.linalg.norm(target_axis)
    if min(source_length,target_length)<.01: raise ValueError('Segmento anatomico troppo corto')
    linear=frame(target_axis,forward)@np.diag([target_length/source_length,1.,1.])@frame(source_axis,forward).T
    result=np.eye(4); result[:3,:3]=linear; result[:3,3]=target_start-linear@start
    return result


def torso_transform(hips, chest, target_hips, target_chest, width_scale=1.):
    """Keep pelvis cross sections upright while matching hip/chest heights."""
    source=chest-hips; target=target_chest-target_hips
    if source[2]<.01 or target[2]<.01: raise ValueError('Altezza del busto non valida')
    linear=np.eye(3);linear[0,0]=width_scale
    linear[:,2]+=(target-linear@source)/source[2]
    result=np.eye(4); result[:3,:3]=linear; result[:3,3]=target_hips-linear@hips
    return result


def skeleton(doc, binary, reference):
    """Fit recognized humanoid chains to the game's bind pose; retain tail bones."""
    from cp77 import world_matrices
    if len(doc.get('skins', [])) != 1:
        raise ValueError('GMod richiede una sola base con un rig umanoide.')
    skin = doc['skins'][0]; nodes = doc['nodes']; world = world_matrices(doc)
    parents = {ch:i for i,n in enumerate(nodes) for ch in n.get('children', [])}
    joints = skin['joints']; names = [nodes[i].get('name', '') for i in joints]
    target = {bone['name']:i for i,bone in enumerate(reference)}
    aliases = [bone_alias(name) for name in names]
    missing = [n for n in ['Pelvis','Head1','L_UpperArm','R_UpperArm','L_Forearm','R_Forearm',
                           'L_Hand','R_Hand','L_Thigh','R_Thigh','L_Calf','R_Calf','L_Foot','R_Foot']
               if 'ValveBiped.Bip01_'+n not in aliases]
    if missing:
        raise ValueError('Mapping umanoide incompleto: ' + ', '.join(missing))
    # Prepared humanoids have left +X, up +Y, forward +Z. ValveBiped's
    # reference SMD has left +X, up +Z, forward -Y (not forward +X).
    conversion = np.array([[1.,0.,0.],[0.,0.,-1.],[0.,1.,0.]])
    positions = [conversion @ world[i][:3,3] * 39.37007874 for i in joints]
    anchor = {a:positions[i] for i,a in enumerate(aliases) if a in target}
    hip_name='ValveBiped.Bip01_Pelvis'
    thighs=['ValveBiped.Bip01_'+side+'_Thigh' for side in ['L','R']]
    # The source Hips controller may be on the abdomen, well above the hip
    # sockets. Use the actual paired thigh origins for anatomical placement.
    anchor[hip_name]=(anchor[thighs[0]]+anchor[thighs[1]])*.5
    hip_width_scale=(np.linalg.norm(reference[target[thighs[0]]]['world'][:3,3]-reference[target[thighs[1]]]['world'][:3,3]) /
                     np.linalg.norm(anchor[thighs[0]]-anchor[thighs[1]]))
    foot_ids=[i for i,name in enumerate(names) if aliases[i] in {
        'ValveBiped.Bip01_L_Foot','ValveBiped.Bip01_R_Foot','ValveBiped.Bip01_L_Toe0','ValveBiped.Bip01_R_Toe0'}
        or name.lower().startswith('toe')]
    ground=[]
    for node_index,node in enumerate(nodes):
        if 'mesh' not in node or node.get('skin')!=0: continue
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            a=primitive['attributes']
            if not all(k in a for k in ['POSITION','JOINTS_0','WEIGHTS_0']): continue
            used=np.unique(b.accessor_array(doc,binary,primitive['indices']).astype(int))
            p=b.accessor_array(doc,binary,a['POSITION'])[used]
            js=b.accessor_array(doc,binary,a['JOINTS_0'])[used]
            ws=b.accessor_array(doc,binary,a['WEIGHTS_0'])[used]
            points=(p@world[node_index][:3,:3].T+world[node_index][:3,3])@conversion.T*39.37007874
            selected=(ws*np.isin(js,foot_ids)).sum(1)>.5
            if selected.any(): ground.extend(points[selected,2])
    floor=float(np.min(ground)) if ground else 0.
    for side in ['L','R']:
        name='ValveBiped.Bip01_'+side+'_Foot'
        anchor[name]=anchor[name].copy()
        anchor[name][2]=floor+reference[target[name]]['world'][2,3]
    def is_descendant(child, ancestor):
        current = reference[target[child]]['parent']
        while current >= 0:
            if current == target[ancestor]: return True
            current = reference[current]['parent']
        return False
    def depth(name):
        result = 0; current = target[name]
        while reference[current]['parent'] >= 0:
            result += 1; current = reference[current]['parent']
        return result
    forward = np.array([0.,-1.,0.])
    def tail_name(n): return bool(re.fullmatch(r'tail(?:rt|root|[0-9]+)?', re.sub('[^a-z0-9]', '', n.lower())))
    transforms = []
    for i, alias in enumerate(aliases):
        if alias not in target:
            transforms.append(None); continue
        start=anchor[alias]; dest = reference[target[alias]]['world'][:3,3]
        # Skip missing intermediate joints and zero-length source segments.
        # Rexouium's Hips and Spine coincide: normalizing their tiny numerical
        # difference used to bend the entire hip-bound surface sideways.
        children = [a for a in anchor if is_descendant(a,alias)
                    and np.linalg.norm(anchor[a]-start) > .01]
        children.sort(key=lambda a:(depth(a),target[a]))
        if alias.endswith('_Pelvis'):
            torso = [a for a in children if any(part in a for part in ['_Spine','_Neck','_Head'])]
            if torso: children = torso
        if children:
            child = children[0]
            transform=segment_transform(start,anchor[child],dest,reference[target[child]]['world'][:3,3],forward)
        else:
            parent = reference[target[alias]]['parent']
            while parent >= 0:
                parentname = reference[parent]['name']
                if parentname in anchor and np.linalg.norm(start-anchor[parentname]) > .01: break
                parent = reference[parent]['parent']
            rotation = (frame(dest-reference[parent]['world'][:3,3], forward) @
                        frame(start-anchor[reference[parent]['name']], forward).T) if parent >= 0 else np.eye(3)
            transform=np.eye(4);transform[:3,:3]=rotation;transform[:3,3]=dest-rotation@start
        if alias.endswith('_Foot'):
            transform=np.eye(4);transform[:3,3]=dest-start
        if alias in {hip_name,'ValveBiped.Bip01_Spine'} and 'ValveBiped.Bip01_Spine2' in anchor:
            chest_name='ValveBiped.Bip01_Spine2'
            transform=torso_transform(anchor[hip_name],anchor[chest_name],
                reference[target[hip_name]]['world'][:3,3],reference[target[chest_name]]['world'][:3,3],hip_width_scale)
        if not np.isfinite(transform).all() or np.linalg.det(transform[:3,:3])<1e-6:
            raise ValueError('Trasformazione anatomica non valida: '+names[i])
        transforms.append(transform)
    # Source paw/toe joints sit close to the ankle, unlike the long human toe
    # lever in ValveBiped. Preserve the paw as one foot-bound surface instead
    # of stretching its toe vertices to a different anatomical joint.
    for i,alias in enumerate(aliases):
        if alias and alias.endswith('_Toe0'):
            foot=alias.removesuffix('Toe0')+'Foot'
            transforms[i]=transforms[aliases.index(foot)].copy();aliases[i]=foot
    remap = []; output = list(reference)
    for i, node in enumerate(joints):
        if aliases[i] in target:
            remap.append(target[aliases[i]]); continue
        ancestor = parents.get(node)
        visited = set()
        while ancestor not in joints and ancestor is not None and ancestor not in visited:
            visited.add(ancestor); ancestor = parents.get(ancestor)
        parent_index = joints.index(ancestor) if ancestor in joints else aliases.index('ValveBiped.Bip01_Pelvis')
        while aliases[parent_index] not in target:
            par = parents.get(joints[parent_index])
            if par not in joints: parent_index = aliases.index('ValveBiped.Bip01_Pelvis'); break
            parent_index = joints.index(par)
        transforms[i] = transforms[parent_index].copy()
        if tail_name(names[i]):
            parent_out = remap[joints.index(ancestor)] if ancestor in joints and joints.index(ancestor)<len(remap) else target['ValveBiped.Bip01_Pelvis']
            mat = np.eye(4); mat[:3,:3] = transforms[i][:3,:3] @ conversion @ world[node][:3,:3]
            u,_,vt = np.linalg.svd(mat[:3,:3]); mat[:3,:3] = u @ vt
            mat[:3,3] = (transforms[i] @ np.r_[positions[i],1])[:3]
            output.append({'name': f'AvatarBridge.Tail{i}', 'parent':parent_out,
                           'local':np.linalg.inv(output[parent_out]['world']) @ mat, 'world':mat})
            remap.append(len(output)-1)
        else:
            remap.append(target[aliases[parent_index]])
    return output, np.array(remap), np.array(transforms), conversion


def write_vtf(image, path):
    image = image.convert('RGBA')
    width, height = [min(2048, 2**int(np.ceil(np.log2(max(1,x))))) for x in image.size]
    image = image.resize((width,height), Image.Resampling.LANCZOS)
    header = bytearray(80)
    struct.pack_into('<4sIIIHHIHH', header, 0, b'VTF\0',7,2,80,width,height,0x2300,1,0)
    struct.pack_into('<3f', header, 32, .5,.5,.5)
    struct.pack_into('<fIBIBBH', header, 48, 1.,0,1,0xFFFFFFFF,0,0,1)
    Path(path).write_bytes(header + image.tobytes())


def materials(doc, binary, folder, namespace):
    folder.mkdir(parents=True, exist_ok=True)
    result = []
    for index, material in enumerate(doc.get('materials', [])):
        pbr = material.get('pbrMetallicRoughness', {})
        texture = pbr.get('baseColorTexture', {}).get('index')
        if texture is None:
            rgba = tuple(int(np.clip(x,0,1)*255) for x in pbr.get('baseColorFactor',[1,1,1,1]))
            image = Image.new('RGBA',(4,4),rgba)
        else:
            image_def = doc['images'][doc['textures'][texture]['source']]
            if 'bufferView' not in image_def:
                raise ValueError('Le texture devono essere incorporate nel GLB.')
            view = doc['bufferViews'][image_def['bufferView']]; start = view.get('byteOffset',0)
            image = Image.open(io.BytesIO(binary[start:start+view['byteLength']])).convert('RGBA')
        name = f'material_{index}'
        write_vtf(image,folder/(name+'.vtf'))
        alpha = '\n "$alphatest" "1"\n "$alphatestreference" "0.4"\n "$nocull" "1"' if material.get('alphaMode') in {'MASK','BLEND'} or image.getchannel('A').getextrema()[0]<255 else ''
        (folder/(name+'.vmt')).write_text('"VertexLitGeneric"\n{\n "$basetexture" "'+namespace+'/'+name+'"\n "$model" "1"'+alpha+'\n}\n',encoding='utf-8')
        result.append(name)
    return result


def smd_header(bones):
    lines = ['version 1','nodes']
    for index,bone in enumerate(bones):
        lines.append(f'{index} "{bone["name"]}" {bone["parent"]}')
    lines += ['end','skeleton','time 0']
    import warnings
    for index,bone in enumerate(bones):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore'); angles = Rotation.from_matrix(bone['local'][:3,:3]).as_euler('xyz')
        values = np.r_[bone['local'][:3,3],angles]
        lines.append(str(index)+' '+' '.join(f'{v:.8f}' for v in values))
    return lines+['end']


def shared_bindings(positions, dense):
    """Share coincident source vertices before limiting Source's three weights.

    UV/material duplicates retain their own normals and UVs, but use exactly
    the same position and final bone weights in every animation.
    """
    _,groups=np.unique(np.round(positions,4),axis=0,return_inverse=True)
    count=np.bincount(groups); size=len(count)
    points=np.zeros((size,3)); weights=np.zeros((size,dense.shape[1]))
    np.add.at(points,groups,positions); np.add.at(weights,groups,dense)
    points/=count[:,None]; weights/=count[:,None]
    joints=np.argsort(-weights,axis=1,kind='stable')[:,:3]
    weights=np.take_along_axis(weights,joints,axis=1)
    weights/=weights.sum(1,keepdims=True)
    return points,joints,weights,groups,int((count>1).sum())


def export_mesh(doc, binary, bones, remap, transforms, conversion, matnames, path, diagnostics=None):
    from cp77 import world_matrices
    world=world_matrices(doc); records=[]; count=0
    target_transforms=np.zeros((len(bones),4,4)); seen=set()
    for source,target in enumerate(remap):
        if target in seen:
            if not np.allclose(target_transforms[target],transforms[source],atol=1e-6):
                raise ValueError('Pesi rimappati con trasformazioni anatomiche diverse')
        else:
            seen.add(int(target)); target_transforms[target]=transforms[source]
    for node_index,node in enumerate(doc['nodes']):
        if 'mesh' not in node: continue
        if node.get('skin')!=0: raise ValueError('La mesh deve essere collegata al rig umanoide.')
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            attributes=primitive['attributes']
            if primitive.get('mode',4)!=4 or not all(k in attributes for k in ['POSITION','NORMAL','TEXCOORD_0','JOINTS_0','WEIGHTS_0']):
                raise ValueError('Mesh triangolata, UV, normali e pesi richiesti.')
            indices=b.accessor_array(doc,binary,primitive['indices']).astype(int).reshape(-1,3)
            used,faces=np.unique(indices,return_inverse=True)
            read=lambda key:b.accessor_array(doc,binary,attributes[key])[used]
            points=read('POSITION'); normals=read('NORMAL'); uv=read('TEXCOORD_0')
            joint=read('JOINTS_0').astype(int); weight=read('WEIGHTS_0').astype(float)
            if not np.isfinite(weight).all() or (weight<0).any() or (weight.sum(1)<=0).any():
                raise ValueError('Pesi scheletro non validi')
            # Merge collapsed source joints before truncation; otherwise three
            # toe/helper weights can exclude an important hip or foot weight.
            dense=np.zeros((len(points),len(bones)))
            np.add.at(dense,(np.arange(len(points))[:,None],remap[joint]),weight)
            transform=world[node_index]
            points=(points@transform[:3,:3].T+transform[:3,3])@conversion.T*39.37007874
            normals=normals@np.linalg.inv(transform[:3,:3])@conversion.T
            records.append(dict(points=points,normals=normals,uv=uv,dense=dense,
                faces=faces.reshape(-1,3),material=matnames[primitive.get('material',0)]))
            count+=len(indices)
    all_points=np.concatenate([r['points'] for r in records]); all_dense=np.concatenate([r['dense'] for r in records])
    points,joints,weights,groups,seams=shared_bindings(all_points,all_dense)
    blend=(target_transforms[joints]*weights[:,:,None,None]).sum(1)
    out=np.einsum('nij,nj->ni',blend[:,:3,:3],points)+blend[:,:3,3]
    normal_transforms=np.linalg.inv(blend[:,:3,:3]).transpose(0,2,1)
    lines=smd_header(bones)+['triangles']; offset=0
    for record in records:
        amount=len(record['points']); ids=groups[offset:offset+amount]; offset+=amount
        normals=np.einsum('nij,nj->ni',normal_transforms[ids],record['normals'])
        normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-10)
        vertexlines=[]
        for vertex,group in enumerate(ids):
            links=[(int(j),float(w)) for j,w in zip(joints[group],weights[group]) if w>1e-7]
            values=np.r_[out[group],normals[vertex],record['uv'][vertex,0],1-record['uv'][vertex,1]]
            vertexlines.append(str(links[0][0])+' '+' '.join(f'{v:.7f}' for v in values)+' '+str(len(links))+' '+' '.join(f'{j} {w:.8f}' for j,w in links))
        for face in record['faces']:
            lines.append(record['material']); lines.extend(vertexlines[i] for i in face)
    Path(path).write_text('\n'.join(lines+['end'])+'\n',encoding='utf-8')
    if diagnostics is not None:
        diagnostics.update(source_vertices=len(all_points),shared_bind_vertices=len(points),
            shared_seam_groups=seams,triangles_preserved=count,
            method='anatomical hip center; connected segment lengths; rigid paws; shared source seams')
    return count


PHYSICS_PARTS = ['Pelvis','Spine2','Head1'] + [side+'_'+part for side in ['L','R']
    for part in ['UpperArm','Forearm','Hand','Thigh','Calf','Foot']]


def tail_dynamics(bones):
    """Align Source's jiggle +Z axis to the tail, with its root anchored."""
    indices = [i for i,bone in enumerate(bones) if bone['name'].startswith('AvatarBridge.Tail')]
    result = []
    for index in indices:
        bone = bones[index]
        children = [i for i in indices if bones[i]['parent']==index]
        parent = bones[bone['parent']]
        direction = (bones[children[0]]['world'][:3,3]-bone['world'][:3,3]) if children else bone['world'][:3,3]-parent['world'][:3,3]
        length = float(np.linalg.norm(direction))
        if length < .01: continue
        bone['world'] = bone['world'].copy()
        bone['world'][:3,:3] = frame(direction,np.array([0.,0.,1.]))[:,[1,2,0]]
        if bone['parent'] in indices:
            result.append({'name':bone['name'],'length':length})
    for index in indices:
        bone=bones[index]
        bone['local']=np.linalg.inv(bones[bone['parent']]['world'])@bone['world']
    return result


def physics_qc(bones, tail):
    names={bone['name'] for bone in bones}
    limits={'Spine2':[(-48,48),(-25,25),(-25,50)],'Head1':[(-20,20),(-25,25),(-20,30)]}
    for side in ['L','R']:
        limits.update({side+'_UpperArm':[(-40,40),(-85,85),(-90,30)],
            side+'_Forearm':[(0,0),(0,0),(-149,4)],side+'_Hand':[(-40,40),(-15,15),(-60,70)],
            side+'_Thigh':[(-12,12),(-73,6) if side=='L' else (-8,75),(-95,32)],
            side+'_Calf':[(0,0),(0,0),(-10,126)],side+'_Foot':[(-10,10),(-20,20),(-15,35)]})
    lines=['$collisionjoints "physics.smd" {','$mass 90','$inertia 10','$damping 0.05','$rotdamping 1.5',
           '$rootbone "ValveBiped.Bip01_Pelvis"','$animatedfriction 1 400 0.5 0.0 0.3']
    for name,axes in limits.items():
        full='ValveBiped.Bip01_'+name
        if full not in names: continue
        for axis,(low,high) in zip('xyz',axes):
            lines.append(f'$jointconstrain "{full}" {axis} limit {low} {high} 0')
    for side in ['L','R']:
        for torso in ['Pelvis','Spine2']:
            lines.append(f'$jointcollide "ValveBiped.Bip01_{side}_Forearm" "ValveBiped.Bip01_{torso}"')
    for part in ['Thigh','Calf','Foot']:
        lines.append(f'$jointcollide "ValveBiped.Bip01_L_{part}" "ValveBiped.Bip01_R_{part}"')
    lines.append('}')
    for item in tail:
        lines += [f'$jigglebone "{item["name"]}" {{ is_flexible {{',
                  f'length {item["length"]:.6f}', 'tip_mass 0.15',
                  'yaw_stiffness 100','yaw_damping 7','pitch_stiffness 120','pitch_damping 7',
                  'along_stiffness 200','along_damping 10','angle_constraint 25','} }']
    return lines


def write_physics(bones, path):
    lines = smd_header(bones)+['triangles']
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    for index,bone in enumerate(bones):
        suffix = bone['name'].removeprefix('ValveBiped.Bip01_')
        if suffix not in PHYSICS_PARTS:
            continue
        start = bone['world'][:3,3]
        children = [b for b in bones if b['parent']==index and 'ValveBiped.Bip01_' in b['name']]
        if suffix.endswith('_Hand'):
            children.sort(key=lambda b:not b['name'].endswith('_Finger2'))
        end = children[0]['world'][:3,3] if children else start+np.array([0,0,5.])
        middle = (start+end)/2
        radius = (1.6,1.6) if suffix.endswith(('_Forearm','_Hand')) else (2.,2.) if suffix.endswith(('_UpperArm','_Calf','_Foot')) else (3.,3.)
        size = np.array([*radius,max(np.linalg.norm(end-start)*.42,1.5)])
        orient = frame(end-start, np.array([1.,0.,0.]))[:,[1,2,0]]
        points = np.array([[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]])*size
        points = points @ orient.T + middle
        for face in faces:
            normal = np.cross(points[face[1]]-points[face[0]],points[face[2]]-points[face[0]]); normal /= np.linalg.norm(normal)
            lines.append('physics')
            for v in face:
                lines.append(f'{index} '+' '.join(f'{x:.7f}' for x in np.r_[points[v],normal,0,0])+f' 1 {index} 1')
    Path(path).write_text('\n'.join(lines+['end'])+'\n',encoding='utf-8')


def build(job_path, game_path):
    from cp77 import load_glb
    root = game_root(game_path); tool = compiler(root); job = Path(job_path).resolve()
    if not job.is_relative_to((b.DATA/'jobs').resolve()):
        raise ValueError('Lavoro fuori dalla libreria locale')
    report = json.loads((job/'report.json').read_text(encoding='utf-8'))
    source = Path(report['output']).resolve()
    if not source.is_relative_to(job): raise ValueError('Modello fuori dalla libreria')
    doc,binary = load_glb(source)
    token = 'avatarbridge_'+job.name.removeprefix('avatar_')
    namespace = 'models/avatarbridge/'+token
    out = job/('gmod_'+uuid.uuid4().hex[:10]); out.mkdir()
    addon = out/'addon'; content = out/'compile-game'; content.mkdir()
    (content/'gameinfo.txt').write_text('"GameInfo" { game "Avatar Bridge build" FileSystem { SteamAppId 4000 SearchPaths { Game "'+str(root/'garrysmod').replace('\\','/')+'" Game "'+str(root/'hl2').replace('\\','/')+'" } } }',encoding='utf-8')
    bones,remap,transforms,conversion = skeleton(doc,binary,reference_bones(root))
    tail = tail_dynamics(bones)
    matnames = materials(doc,binary,addon/'materials'/namespace,namespace)
    geometry_fit={}
    triangles = export_mesh(doc,binary,bones,remap,transforms,conversion,matnames,out/'reference.smd',geometry_fit)
    write_physics(bones,out/'physics.smd')
    (out/'idle.smd').write_text('\n'.join(smd_header(bones))+'\n',encoding='utf-8')
    relative_model = 'avatarbridge/'+token+'/player.mdl'
    qc = [f'$modelname "{relative_model}"', '$body "body" "reference.smd"',f'$cdmaterials "{namespace}"',
        '$surfaceprop "flesh"', '$contents "solid"', '$mostlyopaque', '$maxverts 65536',
        '$sequence "reference" "idle.smd" fps 1', '$sequence "ragdoll" "idle.smd" fps 30 activity ACT_DIERAGDOLL 1', '$includemodel "m_anm.mdl"',
        '$includemodel "m_gst.mdl"', '$includemodel "m_pst.mdl"', '$includemodel "m_shd.mdl"',
        '$attachment "eyes" "ValveBiped.Bip01_Head1" 3 -4 0 rotate 0 -80 -90',
        '$attachment "anim_attachment_RH" "ValveBiped.Bip01_R_Hand" 0 0 0 rotate 0 0 0',
        '$attachment "anim_attachment_LH" "ValveBiped.Bip01_L_Hand" 0 0 0 rotate 0 0 0',
        '$ikchain "rhand" "ValveBiped.Bip01_R_Hand" knee 0.707 0.707 0',
        '$ikchain "lhand" "ValveBiped.Bip01_L_Hand" knee 0.707 0.707 0',
        '$ikchain "rfoot" "ValveBiped.Bip01_R_Foot" knee 0.707 -0.707 0',
        '$ikchain "lfoot" "ValveBiped.Bip01_L_Foot" knee 0.707 -0.707 0',
        *physics_qc(bones,tail)]
    (out/'player.qc').write_text('\n'.join(qc)+'\n',encoding='utf-8')
    proc = subprocess.run([str(tool),'-nop4','-game',str(content),str(out/'player.qc')],cwd=tool.parent,
        capture_output=True,timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    log = (proc.stdout+proc.stderr).decode('utf-8','replace'); (out/'compile.log').write_text(log,encoding='utf-8')
    compiled = content/'models'/relative_model
    if proc.returncode or not compiled.is_file() or re.search(r'(^|\n)\s*ERROR',log,re.I):
        raise ValueError('Compilazione GMod fallita. Log: '+str(out/'compile.log')+'\n'+log[-3500:])
    shutil.copytree(content/'models',addon/'models')
    model = 'models/'+relative_model
    lua = addon/'lua/autorun'/('avatarbridge_'+token+'.lua'); lua.parent.mkdir(parents=True)
    title = report.get('name','Avatar'); category = report.get('category','Avatar Bridge')
    # JSON string quoting is also valid Lua for these user-visible values.
    quote = lambda s: json.dumps(s,ensure_ascii=False).replace('\\/','/')
    lua.write_text('if SERVER then AddCSLuaFile() end\n'+
        f'player_manager.AddValidModel({quote(token)}, {quote(model)}, {quote(title)}, {quote(category)})\n'+
        f'list.Set("PlayerOptionsModel", {quote(title)}, {quote(model)})\n'+
        f'if CLIENT and game.SinglePlayer() then hook.Add("InitPostEntity", "{token}_select", function() RunConsoleCommand("cl_playermodel", "{token}") end) end\n',encoding='utf-8')
    (addon/'addon.json').write_text(json.dumps({'title':title+' - Avatar Bridge','type':'model','tags':['roleplay','fun'],'ignore':['*.log']},indent=2),encoding='utf-8')
    result = {'target':'gmod','builder_version':VERSION,'job':str(job),'addon':str(addon),'token':token,'model':model,
              'triangles':triangles,'bones':len(bones),'tail_jiggle_bones':len(tail),'physics_bodies':len(PHYSICS_PARTS),
              'geometry_fit':geometry_fit,'runtime_verified':False,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    (job/'gmod-build.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def manifest_path(root):
    root = Path(root).resolve()
    key = hashlib.sha256(str(root).lower().encode()).hexdigest()[:16]
    return b.DATA/'installations'/('gmod_'+key+'.json')


def require_closed(root):
    if __import__('os').name != 'nt': return
    text = b.run(['tasklist','/FO','CSV','/NH']).decode('utf-8','replace').lower()
    if any('"'+name+'"' in text for name in ['gmod.exe','gmod_win64.exe','hl2.exe']):
        raise ValueError("Chiudi Garry's Mod prima di installare o rimuovere l'avatar.")


def checked_files(destination, files):
    for name,digest in files.items():
        path = (destination/name).resolve()
        if not path.is_relative_to(destination.resolve()): raise ValueError('Manifest non valido')
        if path.exists() and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest):
            raise ValueError('File modificato: conservato '+str(path))


def install(job_path, game_path):
    root = game_root(game_path); require_closed(root)
    result = build(job_path,root)
    addon = Path(result['addon']); destination = root/'garrysmod/addons'/result['token']
    manifest = manifest_path(root); manifest.parent.mkdir(parents=True,exist_ok=True)
    state = json.loads(manifest.read_text(encoding='utf-8')) if manifest.is_file() else {'addons':{}}
    previous = state['addons'].get(result['token'])
    if destination.exists():
        if not previous: raise ValueError('Cartella addon esistente non gestita da Avatar Bridge: '+str(destination))
        checked_files(destination,previous['files'])
        known = set(previous['files'])
        if any(p.relative_to(destination).as_posix() not in known for p in destination.rglob('*') if p.is_file()):
            raise ValueError('L\'addon contiene altri file: installazione interrotta.')
    temporary = destination.with_name(destination.name+'_staging_'+uuid.uuid4().hex[:8])
    backup = destination.with_name(destination.name+'_backup_'+uuid.uuid4().hex[:8])
    shutil.copytree(addon,temporary)
    files = {p.relative_to(temporary).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in temporary.rglob('*') if p.is_file()}
    installed = False
    try:
        if destination.exists(): destination.rename(backup)
        temporary.rename(destination)
        installed = True
        state['addons'][result['token']] = {'files':files,'model':result['model']}
        tempmanifest = manifest.with_suffix('.tmp'); tempmanifest.write_text(json.dumps(state,indent=2),encoding='utf-8'); tempmanifest.replace(manifest)
    except Exception:
        if installed and destination.exists(): shutil.rmtree(destination)
        if backup.exists(): backup.rename(destination)
        raise
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    if backup.exists(): shutil.rmtree(backup)
    return {**result,'installed':str(destination)}


def uninstall(game_path, job_path=None):
    root = game_root(game_path); require_closed(root); manifest = manifest_path(root)
    if not manifest.is_file(): return {'removed':False}
    state = json.loads(manifest.read_text(encoding='utf-8'))
    token = 'avatarbridge_'+Path(job_path).name.removeprefix('avatar_') if job_path else None
    selected = {k:v for k,v in state['addons'].items() if token is None or k==token}
    for key,entry in selected.items():
        if not re.fullmatch(r'avatarbridge_[a-zA-Z0-9_]+',key): raise ValueError('Manifest non valido')
        checked_files(root/'garrysmod/addons'/key,entry['files'])
    for key,entry in selected.items():
        folder = root/'garrysmod/addons'/key
        for name in entry['files']:
            (folder/name).unlink(missing_ok=True)
        if folder.exists():
            for p in sorted(folder.rglob('*'),key=lambda x:len(x.parts),reverse=True):
                if p.is_dir() and not any(p.iterdir()): p.rmdir()
            if not any(folder.iterdir()): folder.rmdir()
        del state['addons'][key]
    manifest.write_text(json.dumps(state,indent=2),encoding='utf-8')
    return {'removed':list(selected)}
