"""Local original-avatar expression shapes, with bounded Unity parsing."""
import copy
import hashlib
import re
import shutil
import numpy as np
import bridge as b


ALIASES={
    'blink':('blink','eyesclosed','eyeclose','fclallclose'),
    'smile':('smile','fclalljoy','happy'),
    'angry':('growl','angry','fclallangry'),
    'sad':('sad','fclallsorrow'),
    'browdown':('browdown',), 'browup':('browup',),
    'wide':('eyeswide','surprised','fclallsurprised'),
    'open':('mouthopen','v_aa','aa'), 'frown':('frown',)}
BODY_FLAT={'backflat':'Floof_Back_Flat','hipsflat':'Floof_BckHips_Flat',
           'chestflat':'Floof_chest_Flat','shoulderflat':'Floof_Shoulders_Flat','elbowflat':'Floof_Elbows_Flat'}


def canonical(name):
    name=re.sub(r'[^a-z0-9]','',name.lower())
    for key,aliases in ALIASES.items():
        if name==key or name in [re.sub(r'[^a-z0-9]','',a) for a in aliases]:return key
    return None


def unity_deltas(raw,vertex_count,include_body=False):
    """Read only selected expression frames, without loading all YAML vertices.

    The supplied avatar has 50 MB of shape YAML. Parsing its metadata and
    streaming its numeric records avoids allocating millions of YAML objects.
    """
    text=raw.decode('utf-8-sig')
    block=re.search(r'^  m_Shapes:.*?(?=^  m_BindPose:)',text,re.M|re.S)
    if not block:return {}
    text=block[0]
    metadata=re.search(r'^    shapes:.*',text,re.M|re.S)
    if not metadata:return {}
    meta=b.yaml_load(metadata[0]);frames=meta.get('shapes',[]);full=meta.get('fullWeights',[])
    wanted=[];result={}
    for channel in meta.get('channels',[]):
        key=canonical(channel['name'])
        if include_body:
            key=next((k for k,v in BODY_FLAT.items() if v.lower()==channel['name'].lower()),key)
        if key is None or key in result:continue
        frame_id=channel['frameIndex']+channel['frameCount']-1
        if not 0<=frame_id<len(frames):raise ValueError('Frame facciale Unity fuori intervallo')
        frame=frames[frame_id];start=int(frame['firstVertex']);count=int(frame['vertexCount'])
        if start<0 or count<0:raise ValueError('Intervallo facciale Unity non valido')
        delta=np.zeros((vertex_count,3),dtype=np.float32);result[key]=delta
        weight=float(full[frame_id]) if frame_id<len(full) else 100.
        if weight<=0:raise ValueError('Peso facciale Unity non valido')
        wanted.append((start,start+count,delta,100./weight))
    pattern=r'^    - vertex: \{x: ([^,]+), y: ([^,]+), z: ([^}]+)\}\s*\n.*?^      index: (\d+)'
    records=0
    for row,match in enumerate(re.finditer(pattern,text[:metadata.start()],re.M|re.S)):
        records=row+1
        for start,end,delta,factor in wanted:
            if start<=row<end:
                index=int(match[4])
                if not 0<=index<vertex_count:raise ValueError('Vertice facciale Unity fuori intervallo')
                vector=np.array([float(match[i]) for i in [1,2,3]])*factor
                if not np.isfinite(vector).all():raise ValueError('Delta facciale non finito')
                vector[0]*=-1 # Unity -> glTF, same handedness correction as the base.
                delta[index]=vector
    if any(end>records for _,end,_,_ in wanted):raise ValueError('Buffer facciale Unity incompleto')
    return result


def add_unity_targets(raw,vertex_count,glb,primitives):
    deltas=unity_deltas(raw,vertex_count,include_body=True)
    flattened=[deltas.pop(key) for key in BODY_FLAT if key in deltas]
    if flattened and primitives:
        # UV seams split real skin into small disconnected islands too. A
        # flattening shape is a displacement, never permission to delete faces.
        glb.doc.setdefault('extras',{})['removed_original_fur_triangles']=0
        accessor=primitives[0]['attributes']['POSITION']
        positions=b.accessor_array(glb.doc,glb.bin,accessor).copy()+np.sum(flattened,axis=0)
        accessor=glb.array(positions,'VEC3',bounds=True,target=34962)
        for primitive in primitives:primitive['attributes']['POSITION']=accessor
    targets=[{'POSITION':glb.array(delta,'VEC3',target=34962)} for delta in deltas.values()]
    if targets:
        for primitive in primitives:primitive['targets']=copy.deepcopy(targets)
    return list(deltas)


def recipes(available):
    available=set(available);result=[('neutral','Neutro',{})]
    choices=[('smile','Sorriso',{'smile':1.}),('angry','Arrabbiato',{'angry':.8,'browdown':.65}),
             ('sad','Triste',{'sad':1.,'frown':.4}),('surprised','Sorpreso',{'wide':1.,'browup':.6,'open':.45})]
    for key,label,values in choices:
        values={k:v for k,v in values.items() if k in available}
        if values:result.append((key,label,values))
    return result


def build(template,head_path,primitives,materials,textures,directory,prefix):
    """Cook original expression geometry; retain the neutral head's skin/UVs."""
    import cp77 as c
    doc,binary=c.load_glb(head_path)
    available=set().union(*(p.get('facial',{}) for p in primitives))
    if not available:return None
    expressions=[]
    for key,label,values in recipes(available):
        states=[]
        for blink in ([0.,.5,1.] if 'blink' in available else [0.]):
            if key=='neutral' and blink==0.:
                states.append(prefix+'\\head.mesh');continue
            writer=b.GLB();writer.doc=copy.deepcopy(doc);writer.bin=bytearray(binary)
            for mesh,primitive in zip(writer.doc['meshes'],primitives):
                target=mesh['primitives'][0];attrs=target['attributes']
                pos=primitive['positions'].copy();weights=dict(values)
                if blink:weights['blink']=blink
                for name,weight in weights.items():
                    if name in primitive.get('facial',{}):pos+=primitive['facial'][name]*weight
                tri=primitive['indices'].reshape(-1,3);normals=np.zeros_like(pos)
                cross=np.cross(pos[tri[:,1]]-pos[tri[:,0]],pos[tri[:,2]]-pos[tri[:,0]])
                for column in range(3):np.add.at(normals,tri[:,column],cross)
                lengths=np.linalg.norm(normals,axis=1);valid=lengths>1e-10
                normals[valid]/=lengths[valid,None];normals[~valid]=primitive['normals'][~valid]
                attrs['POSITION']=writer.array(pos,'VEC3',bounds=True,target=34962)
                attrs['NORMAL']=writer.array(normals,'VEC3',target=34962)
                attrs['TANGENT']=writer.array(c.tangents(pos,normals,primitive['uv'],primitive['indices']),'VEC4',target=34962)
            filename=f'face_{key}_{int(blink*2)}';path=directory/(filename+'.glb');writer.save(path)
            output=directory/(filename+'.mesh')
            shutil.copy2(directory/'head.mesh',output)
            before=hashlib.sha256(output.read_bytes()).hexdigest()
            c.command('import',path,'-o',directory,'--keep')
            if hashlib.sha256(output.read_bytes()).hexdigest()==before:
                raise ValueError('Espressione non importata: '+filename)
            states.append(prefix+'\\'+filename+'.mesh')
        expressions.append({'id':key,'name':label,'states':states})
    return {'expressions':expressions,'blink':'blink' in available,
            'method':'Original avatar shapes, selectable expression and automatic blink; no dialogue lip sync.'}
