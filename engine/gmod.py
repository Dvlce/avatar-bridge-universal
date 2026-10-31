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

VERSION = 1


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
    x = axis / max(np.linalg.norm(axis), 1e-10)
    z = forward - x * (x @ forward)
    if np.linalg.norm(z) < 1e-5:
        forward = np.array([0., 1., 0.]); z = forward - x * (x @ forward)
    z /= max(np.linalg.norm(z), 1e-10)
    return np.column_stack([x, np.cross(z, x), z])


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
    # glTF +Y up/+Z backwards -> Source +Z up/+X forward. Det = +1.
    conversion = np.array([[0.,0.,-1.],[-1.,0.,0.],[0.,1.,0.]])
    positions = [conversion @ world[i][:3,3] * 39.37007874 for i in joints]
    anchor = {a:positions[i] for i,a in enumerate(aliases) if a in target}
    def tail_name(n): return bool(re.fullmatch(r'tail(?:rt|root|[0-9]+)?', re.sub('[^a-z0-9]', '', n.lower())))
    transforms = []
    for i, alias in enumerate(aliases):
        if alias not in target:
            transforms.append(None); continue
        dest = reference[target[alias]]['world'][:3,3]
        children = [a for a in anchor if reference[target[a]]['parent'] == target[alias]]
        if not children:
            descendant = [a for a in anchor if target[a] > target[alias] and a.startswith(alias.split('_Finger')[0]+'_Finger')]
            children = descendant[:1] if '_Hand' in alias else []
        if children:
            child = children[0]
            rotation = frame(reference[target[child]]['world'][:3,3]-dest, np.array([1.,0.,0.])) @ frame(anchor[child]-positions[i], np.array([1.,0.,0.])).T
        else:
            parent = reference[target[alias]]['parent']
            parentname = reference[parent]['name'] if parent >= 0 else None
            rotation = (frame(dest-reference[parent]['world'][:3,3], np.array([1.,0.,0.])) @
                        frame(positions[i]-anchor[parentname], np.array([1.,0.,0.])).T) if parentname in anchor else np.eye(3)
        transform = np.eye(4); transform[:3,:3] = rotation; transform[:3,3] = dest - rotation @ positions[i]
        transforms.append(transform)
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


def export_mesh(doc, binary, bones, remap, transforms, conversion, matnames, path):
    from cp77 import world_matrices
    world = world_matrices(doc); lines = smd_header(bones)+['triangles']; count = 0
    for node_index,node in enumerate(doc['nodes']):
        if 'mesh' not in node: continue
        if node.get('skin') != 0:
            raise ValueError('La mesh deve essere collegata al rig umanoide.')
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            attributes = primitive['attributes']
            if primitive.get('mode',4)!=4 or not all(k in attributes for k in ['POSITION','NORMAL','TEXCOORD_0','JOINTS_0','WEIGHTS_0']):
                raise ValueError('Mesh triangolata, UV, normali e pesi richiesti.')
            read = lambda key:b.accessor_array(doc,binary,attributes[key])
            p = read('POSITION'); normals = read('NORMAL'); uv = read('TEXCOORD_0')
            joint = read('JOINTS_0').astype(int); weight = read('WEIGHTS_0').astype(float)
            if not np.isfinite(weight).all() or (weight<0).any() or (weight.sum(1)<=0).any():
                raise ValueError('Pesi scheletro non validi')
            order = np.argsort(-weight,axis=1)[:,:3]; joint = np.take_along_axis(joint,order,1); weight = np.take_along_axis(weight,order,1)
            weight /= weight.sum(1,keepdims=True)
            transform = world[node_index]
            p = ((p @ transform[:3,:3].T)+transform[:3,3]) @ conversion.T * 39.37007874
            normals = normals @ np.linalg.inv(transform[:3,:3]) @ conversion.T
            out = np.zeros_like(p); outnormal = np.zeros_like(normals)
            for column in range(3):
                tr = transforms[joint[:,column]]
                out += (np.einsum('nij,nj->ni',tr[:,:3,:3],p)+tr[:,:3,3])*weight[:,column,None]
                outnormal += np.einsum('nij,nj->ni',tr[:,:3,:3],normals)*weight[:,column,None]
            outnormal /= np.maximum(np.linalg.norm(outnormal,axis=1,keepdims=True),1e-10)
            faces = b.accessor_array(doc,binary,primitive['indices']).astype(int).reshape(-1,3)
            vertexlines = []
            for vertex in range(len(p)):
                influences = {}
                for j,w in zip(remap[joint[vertex]],weight[vertex]):
                    influences[int(j)] = influences.get(int(j),0)+float(w)
                links = [(j,w) for j,w in influences.items() if w>1e-7]
                values = np.r_[out[vertex],outnormal[vertex],uv[vertex,0],1-uv[vertex,1]]
                vertexlines.append(str(links[0][0])+' '+' '.join(f'{v:.7f}' for v in values)+' '+str(len(links))+' '+' '.join(f'{j} {w:.8f}' for j,w in links))
            for face in faces:
                lines.append(matnames[primitive.get('material',0)])
                lines.extend(vertexlines[i] for i in face)
            count += len(faces)
    lines.append('end'); Path(path).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return count


def write_physics(bones, path):
    lines = smd_header(bones)+['triangles']
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    for index,bone in enumerate(bones):
        suffix = bone['name'].removeprefix('ValveBiped.Bip01_')
        if suffix not in ['Pelvis','Spine2','Head1','L_UpperArm','R_UpperArm','L_Forearm','R_Forearm','L_Thigh','R_Thigh','L_Calf','R_Calf']:
            continue
        start = bone['world'][:3,3]
        children = [b for b in bones if b['parent']==index and 'ValveBiped.Bip01_' in b['name']]
        end = children[0]['world'][:3,3] if children else start+np.array([0,0,5.])
        middle = (start+end)/2
        size = np.array([3.,3.,max(np.linalg.norm(end-start)*.35,2.)])
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
    matnames = materials(doc,binary,addon/'materials'/namespace,namespace)
    triangles = export_mesh(doc,binary,bones,remap,transforms,conversion,matnames,out/'reference.smd')
    write_physics(bones,out/'physics.smd')
    (out/'idle.smd').write_text('\n'.join(smd_header(bones))+'\n',encoding='utf-8')
    relative_model = 'avatarbridge/'+token+'/player.mdl'
    qc = [f'$modelname "{relative_model}"', '$body "body" "reference.smd"',f'$cdmaterials "{namespace}"',
        '$surfaceprop "flesh"', '$contents "solid"', '$mostlyopaque', '$maxverts 65536',
        '$sequence "reference" "idle.smd" fps 1', '$includemodel "m_anm.mdl"',
        '$includemodel "m_gst.mdl"', '$includemodel "m_pst.mdl"', '$includemodel "m_shd.mdl"',
        '$attachment "eyes" "ValveBiped.Bip01_Head1" 3 -4 0 rotate 0 -80 -90',
        '$attachment "anim_attachment_RH" "ValveBiped.Bip01_R_Hand" 0 0 0 rotate 0 0 0',
        '$attachment "anim_attachment_LH" "ValveBiped.Bip01_L_Hand" 0 0 0 rotate 0 0 0',
        '$ikchain "rhand" "ValveBiped.Bip01_R_Hand" knee 0.707 0.707 0',
        '$ikchain "lhand" "ValveBiped.Bip01_L_Hand" knee 0.707 0.707 0',
        '$ikchain "rfoot" "ValveBiped.Bip01_R_Foot" knee 0.707 -0.707 0',
        '$ikchain "lfoot" "ValveBiped.Bip01_L_Foot" knee 0.707 -0.707 0',
        '$collisionjoints "physics.smd" { $mass 90 $inertia 10 $damping 0.01 $rotdamping 1.5 $rootbone "ValveBiped.Bip01_Pelvis" }']
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
              'triangles':triangles,'bones':len(bones),'runtime_verified':False,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
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
