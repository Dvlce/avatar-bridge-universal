"""Local geometry fitting; no executable content is read from avatar assets."""
import numpy as np
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
import bridge as b


def fit_pelvis_profile(positions, normals, hip_weights, anchors):
    """Shorten a hip-bound front tuft without removing skin or joining thighs.

    Retargeting a long VRChat hip shell onto V's pelvis can leave an apron down
    between the thighs. Compress only that central, forward, hip-bound surface;
    the mapping stays monotonic and preserves every triangle and UV coordinate.
    """
    p = np.asarray(positions, dtype=float)
    n = np.asarray(normals, dtype=float)
    hip = np.asarray(anchors['Hips'])
    hip = hip[:3, 3] if hip.ndim == 2 else hip
    left = np.asarray(anchors['LeftUpLeg']); right = np.asarray(anchors['RightUpLeg'])
    left = left[:3, 3] if left.ndim == 2 else left
    right = right[:3, 3] if right.ndim == 2 else right
    half_width = max(abs(float(left[0]-right[0]))*.5, .04)
    def smooth(value):
        value = np.clip(value, 0, 1)
        return value*value*(3-2*value)
    def warp(points):
        central = smooth((half_width*1.15-abs(points[:,0]-hip[0]))/(half_width*.6))
        front = smooth((hip[2]-.035-points[:,2])/.045)
        # The inner thighs and pubic surface share vertices with very different
        # hip weights. A weight-dependent displacement folded this continuous
        # surface into a short flap. Use one spatial field across that seam.
        lower = smooth((points[:,1]-(hip[1]-.38))/.11)
        # A C1 hinge avoids a crease at the start of the compact profile.
        extra = np.maximum(hip[1]-.105-points[:,1], 0)
        transition = .025
        hinge = np.where(extra < transition, extra*extra/(2*transition), extra-transition/2)
        result = points.copy()
        result[:,1] += .94*hinge*central*front*lower
        return result
    result = warp(p)
    moved = np.linalg.norm(result-p, axis=1)>1e-8
    output_normals = n.copy()
    if moved.any():
        derivatives = []
        for axis in range(3):
            delta = np.zeros(3); delta[axis] = 1e-5
            derivatives.append((warp(p+delta)[:,1]-warp(p-delta)[:,1])/(2e-5))
        dx,dy,dz = derivatives
        output_normals[:,1] = n[:,1]/np.maximum(dy, .055)
        output_normals[:,0] = n[:,0]-dx*output_normals[:,1]
        output_normals[:,2] = n[:,2]-dz*output_normals[:,1]
        output_normals /= np.maximum(np.linalg.norm(output_normals,axis=1,keepdims=True),1e-10)
        output_normals[~moved] = n[~moved]
    return result, output_normals, {'moved_vertices':int(moved.sum()),
        'max_displacement_m':float(np.linalg.norm(result-p,axis=1).max(initial=0)),
        'method':'continuous spatial compression across pelvis and inner-thigh seam; intact topology'}


def fit_dorsal_shells(positions,normals,uv,triangles,joints,weights,anchors,joint_names=None,excluded_vertices=None):
    """Lay dorsal shells onto intact skin, retaining every face and neckline.

    Project along the anatomical back axis, never onto a nearest triangle.
    The validated principal torso patch remains unchanged. Unfamiliar models
    without an independent complete skin patch retain their geometry.
    """
    from garments import torso_skin_surface
    from scipy.spatial import ConvexHull,QhullError
    def point(name):
        value=np.asarray(anchors[name]);return value[:3,3] if value.ndim==2 else value
    hips,neck=point('Hips'),point('Neck')
    width=max(abs(point(side+'Arm')[0]-hips[0]) for side in ['Left','Right'])
    centers=positions[triangles].mean(1)
    selected=np.flatnonzero((centers[:,1]>hips[1]-.05)&(centers[:,1]<neck[1]+.02)&
                            (abs(centers[:,0]-hips[0])<width*1.1)&
                            (abs(positions[triangles,2]-hips[2]).max(1)<(neck[1]-hips[1])*.65))
    if joint_names is not None:
        from garments import group
        labels=np.array([group(name) for name in joint_names])
        dominant=labels[joints[np.arange(len(joints)),weights.argmax(1)]]
        selected=selected[(dominant[triangles[selected]]=='torso').any(1)]
    core=torso_skin_surface(positions.astype(np.float32),triangles,uv,selected,{n:point(n) for n in anchors},minimum_share=.40)
    if not len(core) or len(core)==len(selected):
        return positions,normals,joints,weights,{'moved_vertices':0,'method':'no independent dorsal shell'}
    # The nape crosses the body/head split. Find the separate, continuous
    # neck skin patch and protect it too, so the taper can finish above a
    # collar instead of leaving a protruding strip at shoulder height.
    _,remap=np.unique(np.column_stack([np.round(positions.astype(np.float32),5),np.round(uv,6)]),axis=0,return_inverse=True)
    local=remap[triangles];edges=np.concatenate([local[:,[0,1]],local[:,[1,2]],local[:,[2,0]]])
    count,components=connected_components(coo_matrix((np.ones(len(edges)),(edges[:,0],edges[:,1])),
                                          shape=(int(remap.max())+1,)*2),directed=False)
    labels=components[local[:,0]];head=point('Head') if 'Head' in anchors else neck+np.array([0,.14,0])
    neck_core=np.empty(0,dtype=int);largest=0.
    for component in np.unique(labels):
        ids_component=np.flatnonzero(labels==component);points=positions[np.unique(triangles[ids_component])]
        low,high_bound=points.min(0),points.max(0)
        if not (neck[1]-.10<low[1]<neck[1]+.025 and head[1]+.025<high_bound[1]<head[1]+.16 and
                low[0]<neck[0]-.035 and high_bound[0]>neck[0]+.035 and
                low[2]<neck[2]-.035 and high_bound[2]>neck[2]+.005):continue
        tri=positions[triangles[ids_component]];area=float(np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1).sum())
        if area>largest:neck_core=ids_component;largest=area
    skin=triangles[np.union1d(core,neck_core)];surface=positions[skin];protected=np.unique(skin)
    high=min(float(surface[:,:,1].max())-.004,float(head[1]+.025 if len(neck_core) else neck[1]+.02))
    taper_start=neck[1]+.05 if len(neck_core) else neck[1]-.055
    ids=np.flatnonzero((positions[:,1]>hips[1]+.015)&(positions[:,1]<high)&
                       (positions[:,2]>hips[2]+.02)&(positions[:,2]<hips[2]+(neck[1]-hips[1])*.65)&
                       (abs(positions[:,0]-hips[0])<width*1.1))
    if joint_names is not None:
        tail_ids=[i for i,name in enumerate(joint_names) if name.startswith('dyng_avatar_tail_')]
        tail_weight=(weights*np.isin(joints,tail_ids)).sum(1)
        ids=ids[np.isin(dominant[ids],['torso','head'])&(tail_weight[ids]<.025)]
    ids=ids[np.isin(ids,np.unique(triangles))&~np.isin(ids,protected)]
    if excluded_vertices is not None:ids=ids[~np.isin(ids,excluded_vertices)]
    p=positions.copy();n=normals.copy();j=joints.copy();w=weights.copy();moved=[]
    low_y=surface[:,:,1].min(1);high_y=surface[:,:,1].max(1)
    spine=[name for name in ['Hips','Spine1','Spine3','Neck'] if joint_names is not None and name in joint_names]
    def posterior_section(x,y):
        crossing=surface[(low_y<=y)&(high_y>=y)];points=[]
        for first,last in [(0,1),(1,2),(2,0)]:
            a=crossing[:,first];delta=crossing[:,last]-a
            valid=((a[:,1]-y)*(a[:,1]+delta[:,1]-y)<=0)&(abs(delta[:,1])>1e-10)
            points.extend((a[valid]+delta[valid]*((y-a[valid,1])/delta[valid,1])[:,None])[:,[0,2]])
        if len(points)<4:return None
        points=np.asarray(points)
        # A continuous anatomical envelope bridges the UV-separated central
        # back skin. This hull is for shaping only, never garment coverage.
        try:polygon=points[ConvexHull(points).vertices]
        except QhullError:return None
        edge=np.roll(polygon,-1,axis=0)-polygon
        fraction=np.divide(x-polygon[:,0],edge[:,0],out=np.full(len(edge),-1.),where=abs(edge[:,0])>1e-10)
        valid=(fraction>=0)&(fraction<=1)
        if not valid.any():return None
        back=float((polygon[:,1]+fraction*edge[:,1])[valid].max())
        return back if back>=hips[2]+.025 else None
    # A UV-separated torso can contain only the front of an upper slice.
    # Its convex hull then closes across the chest, not the anatomical back.
    # Bridge only a bounded vertical gap between real posterior sections of
    # the protected skin; never extrapolate or use the fur as a reference.
    levels=np.linspace(float(low_y.min())+.001,high-.001,96)
    section_cache={}
    bridged=0
    for vertex in ids:
        x,y,z=positions[vertex];back=posterior_section(x,y)
        if back is None:
            key=round(float(x),5)
            if key not in section_cache:
                section_cache[key]=[(height,value) for height in levels
                                    if (value:=posterior_section(x,height)) is not None]
            samples=section_cache[key]
            below=[sample for sample in samples if sample[0]<y]
            above=[sample for sample in samples if sample[0]>y]
            if not below or not above:continue
            first,last=below[-1],above[0]
            if last[0]-first[0]>.30:continue
            back=float(np.interp(y,[first[0],last[0]],[first[1],last[1]]))
            bridged+=1
        if z<=back+.004:continue
        fade=float(np.clip((high-y)/max(high-taper_start,.015),0,1))
        fade=fade*fade*(3-2*fade)
        p[vertex,2]+=(back+.003-z)*fade
        n[vertex]=normals[vertex]*(1-fade)+np.array([0.,0.,1.])*fade
        n[vertex]/=max(float(np.linalg.norm(n[vertex])),1e-10)
        if len(spine)>=2:
            combined={}
            for joint,weight in zip(joints[vertex],weights[vertex]*(1-fade)):
                combined[int(joint)]=combined.get(int(joint),0)+float(weight)
            heights=np.array([point(name)[1] for name in spine]);upper=int(np.clip(np.searchsorted(heights,y),1,len(spine)-1))
            blend=float(np.clip((y-heights[upper-1])/max(heights[upper]-heights[upper-1],1e-8),0,1))
            for name,amount in [(spine[upper-1],1-blend),(spine[upper],blend)]:
                joint=joint_names.index(name);combined[joint]=combined.get(joint,0)+amount*fade
            top=sorted(combined.items(),key=lambda item:-item[1])[:4]
            j[vertex]=0;w[vertex]=0
            for column,(joint,weight) in enumerate(top):j[vertex,column]=joint;w[vertex,column]=weight
            w[vertex]/=max(float(w[vertex].sum()),1e-10)
        moved.append(int(vertex))
    assert np.array_equal(p[protected],positions[protected])
    return p,n,j,w,{'moved_vertices':len(moved),'max_displacement_m':float(np.linalg.norm(p-positions,axis=1).max()),
                     'protected_skin_vertices':len(protected),'triangles_preserved':len(triangles),
                     'protected_neck_triangles':len(neck_core),
                     'bridged_posterior_vertices':bridged,
                     'method':'dorsal axis cap to principal skin cross sections; spine binding; neckline fade'}


def body_regions(positions,triangles,anchors,arm_weights=None,hip_weights=None):
    """Stable native body sections; retain every triangle and exposed paws."""
    def point(name):
        value=np.asarray(anchors[name]);return value[:3,3] if value.ndim==2 else value
    p=positions[triangles].mean(axis=1);hips=point('Hips');neck=point('Neck')
    regions=np.full(len(triangles),'keep',dtype='<U20')
    shoulder_width=max(abs(point(side+'Arm')[0]-hips[0]) for side in ['Left','Right'])
    torso=(p[:,1]>hips[1]-.12)&(p[:,1]<neck[1]+.12)&(abs(p[:,0]-hips[0])<shoulder_width*1.10)
    regions[torso]='torso_upper'
    regions[torso&(p[:,1]<point('Spine3')[1]-.04)]='torso_middle'
    regions[torso&(p[:,1]<point('Spine1')[1])]='torso_lower'
    regions[torso&(p[:,1]<hips[1]+.035)]='hips'
    for side in ['Left','Right']:
        label='l' if side=='Left' else 'r'
        upper=point(side+'UpLeg');foot=point(side+'Foot')
        leg_side=(p[:,0]-hips[0])*(upper[0]-hips[0])>=0
        leg=leg_side&(p[:,1]<hips[1]-.12)&(positions[triangles,1].min(axis=1)>foot[1]+.055)
        # Place a triangle crossing a cuff in the more distal section. Hiding
        # the proximal section must leave overlap behind the garment's hem.
        fraction=(hips[1]-positions[triangles,1].min(axis=1))/max(hips[1]-foot[1],1e-6)
        for i,(low,high) in enumerate(zip([0,.27,.48,.70],[.27,.48,.70,1.1])):
            regions[leg&(fraction>=low)&(fraction<high)]=label+'leg_'+str(i)
        arm=point(side+'Arm');hand=point(side+'Hand');axis=hand-arm;length=axis@axis
        fraction=(p-arm)@axis/max(length,1e-8)
        distance=np.linalg.norm((p-arm)-fraction[:,None]*axis,axis=1)
        extremes=(positions[triangles]-arm)@axis/max(length,1e-8)
        fraction=extremes.max(axis=1)
        selected=(extremes.min(axis=1)>-.04)&(fraction<.94)&(distance<.22)
        if arm_weights is not None:selected&=arm_weights[triangles].mean(axis=1)>.3
        for i,(low,high) in enumerate(zip([-.04,.45,.73],[.45,.73,.94])):
            regions[selected&(fraction>=low)&(fraction<high)]=label+'arm_'+str(i)
    covered=covered_pelvis_triangles(positions,triangles,anchors,hip_weights)
    regions[covered]='pelvis'
    return regions


def covered_pelvis_triangles(positions,triangles,anchors,hip_weights=None):
    """Small central front pelvis patch, hidden only with lower clothing."""
    hips=np.asarray(anchors['Hips'])[:3,3] if np.asarray(anchors['Hips']).ndim==2 else np.asarray(anchors['Hips'])
    left=np.asarray(anchors['LeftUpLeg']);right=np.asarray(anchors['RightUpLeg'])
    left=left[:3,3] if left.ndim==2 else left;right=right[:3,3] if right.ndim==2 else right
    width=abs(float(left[0]-right[0]))*.44
    centers=positions[triangles].mean(axis=1)
    result=((abs(centers[:,0]-hips[0])<width)&(centers[:,1]>hips[1]-(.42 if hip_weights is not None else .25))&
            (centers[:,1]<hips[1]-.015)&(centers[:,2]<hips[2]-.045))
    if hip_weights is not None:result&=np.asarray(hip_weights)[triangles].mean(axis=1)>.55
    return result


def dorsal_crest_triangles(positions, triangles, anchors):
    """Remove entire dorsal feather islands; never cut the skin or tail.

    Coordinates are avatar world coordinates after its facing correction.
    Only call this for a separately labelled feather/fur material.
    """
    triangles=np.asarray(triangles)
    keep=np.ones(len(triangles),dtype=bool)
    if not len(triangles) or not {'Hips','Chest','Neck','Head'}.issubset(anchors):return keep
    hips,chest,neck,head=[np.asarray(anchors[n]) for n in ['Hips','Chest','Neck','Head']]
    size=max(float(np.linalg.norm(neck-chest)),1e-6)
    width=max(size*.55,abs(float(np.asarray(anchors.get('Left shoulder',neck))[0]-np.asarray(anchors.get('Right shoulder',neck))[0]))*.65)
    used,indices=np.unique(triangles,return_inverse=True);local=indices.reshape(-1,3)
    edges=np.concatenate([local[:,[0,1]],local[:,[1,2]],local[:,[2,0]]])
    graph=coo_matrix((np.ones(len(edges)),(edges[:,0],edges[:,1])),shape=(len(used),len(used)))
    count,labels=connected_components(graph,directed=False)
    removed=np.zeros(count,dtype=bool)
    for island in range(count):
        points=positions[used[labels==island]];center=points.mean(axis=0)
        removed[island]=(points[:,1].min()>hips[1]+size*.10 and
                         points[:,1].min()<head[1]+size*.45 and
                         center[2]>neck[2]+size*.10 and
                         np.abs(points[:,0]-neck[0]).max()<width)
    return ~removed[labels[local[:,0]]]


def remove_dorsal_crest(path,output):
    import cp77 as c
    doc,binary=c.load_glb(path);writer=b.GLB();writer.doc=doc;writer.bin=bytearray(binary)
    if len(doc.get('skins',[]))!=1:
        writer.save(output);return 0
    world=c.world_matrices(doc);mesh_node=next(i for i,n in enumerate(doc['nodes']) if 'mesh' in n)
    transform=np.diag([-1.,1.,-1.,1.])@world[mesh_node]
    skin=doc['skins'][0];ibms=b.accessor_array(doc,binary,skin['inverseBindMatrices']).reshape(-1,4,4).transpose(0,2,1)
    poses=transform@np.linalg.inv(ibms)
    anchors={doc['nodes'][n]['name']:poses[i,:3,3] for i,n in enumerate(skin['joints'])}
    removed=0
    for mesh in doc['meshes']:
        for primitive in mesh['primitives']:
            material=primitive.get('material')
            label=(doc.get('materials',[])[material].get('name','').lower()
                   if material is not None and 0<=material<len(doc.get('materials',[])) else '')
            if not any(word in label for word in ['feather','fluff','fur','crest','mane']) or any(word in label for word in ['body','skin','eye']):continue
            pos=b.accessor_array(doc,binary,primitive['attributes']['POSITION'])
            pos=pos@transform[:3,:3].T+transform[:3,3]
            tri=b.accessor_array(doc,binary,primitive['indices']).reshape(-1,3)
            keep=dorsal_crest_triangles(pos,tri,anchors);removed+=int((~keep).sum())
            primitive['indices']=writer.array(tri[keep].reshape(-1),'SCALAR',5125,target=34963)
        mesh['primitives']=[p for p in mesh['primitives'] if writer.doc['accessors'][p['indices']]['count']]
    writer.save(output)
    return removed


def accessory_weights(doc,binary):
    """Bind each fur island to its skin roots, rather than a distant hair bone.

    Keeping the island's original positions preserves the shape. Matching its
    root weights to the skin prevents a head-bound back crest floating away
    from the chest when the character bends or looks around.
    """
    primitives=[p for mesh in doc['meshes'] for p in mesh['primitives']]
    materials=doc.get('materials',[])
    def label(p):return materials[p.get('material',0)].get('name','').lower()
    accessory=[p for p in primitives if any(n in label(p) for n in ['feather','fluff','hair','fur'])
               and not any(n in label(p) for n in ['body','skin','eye'])]
    core=[p for p in primitives if p not in accessory and any(n in label(p) for n in ['body','skin'])]
    if not accessory or not core:return {},{'islands':0}
    points=[];joints=[];weights=[]
    for p in core:
        a=p['attributes'];used=np.unique(b.accessor_array(doc,binary,p['indices']))
        points.append(b.accessor_array(doc,binary,a['POSITION'])[used])
        joints.append(b.accessor_array(doc,binary,a['JOINTS_0'])[used])
        weights.append(b.accessor_array(doc,binary,a['WEIGHTS_0'])[used])
    points=np.concatenate(points);joints=np.concatenate(joints);weights=np.concatenate(weights)
    tree=cKDTree(points);result={};islands=0
    for p in accessory:
        a=p['attributes'];positions=b.accessor_array(doc,binary,a['POSITION'])
        used,indices=np.unique(b.accessor_array(doc,binary,p['indices']),return_inverse=True)
        triangles=indices.reshape(-1,3)
        edges=np.concatenate([triangles[:,[0,1]],triangles[:,[1,2]],triangles[:,[2,0]]])
        graph=coo_matrix((np.ones(len(edges)),(edges[:,0],edges[:,1])),shape=(len(used),len(used)))
        count,labels=connected_components(graph,directed=False)
        distance,nearest=tree.query(positions[used])
        new_joints=b.accessor_array(doc,binary,a['JOINTS_0']).copy().astype(np.uint16)
        new_weights=b.accessor_array(doc,binary,a['WEIGHTS_0']).copy().astype(np.float32)
        for island in range(count):
            members=np.flatnonzero(labels==island)
            roots=members[np.argsort(distance[members])[:min(4,len(members))]]
            dense=np.zeros(len(doc['skins'][0]['joints']))
            for root in roots:np.add.at(dense,joints[nearest[root]],weights[nearest[root]]/len(roots))
            strongest=np.argsort(-dense)[:4];influence=dense[strongest]
            influence/=max(influence.sum(),1e-10)
            new_joints[used[members]]=strongest;new_weights[used[members]]=influence
        result[id(p)]=(new_joints,new_weights);islands+=count
    return result,{'islands':islands,'method':'Fur islands follow the original body weights at their nearest skin roots.'}
