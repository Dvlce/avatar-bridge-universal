"""Build player-only garment refits against the imported body's skin envelope."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import re
import shutil
import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial import ConvexHull, QhullError
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.sparse import coo_matrix, diags, eye
from scipy.sparse.linalg import factorized
from scipy.sparse.csgraph import connected_components
import bridge as b


def torso_skin_surface(vertices,triangles,uv,selected,anchors,minimum_share=.50):
    """Fit to the main skin patch while keeping fur and every body face intact.

    Position + UV reconnect duplicates introduced by body sections without
    joining decorative shells to the skin at coincident root positions.
    Ambiguous or incomplete torso patches retain the original envelope.
    """
    if not len(selected):return selected
    _,remap=np.unique(np.column_stack([np.round(vertices,5),np.round(uv,6)]),axis=0,return_inverse=True)
    local=remap[triangles]
    edges=np.concatenate([local[:,[0,1]],local[:,[1,2]],local[:,[2,0]]])
    count,labels=connected_components(coo_matrix((np.ones(len(edges)),(edges[:,0],edges[:,1])),
                                     shape=(int(remap.max())+1,int(remap.max())+1)),directed=False)
    ids=labels[local[:,0]]
    tri=vertices[triangles];area=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)
    sizes=np.bincount(ids[selected],weights=area[selected],minlength=count);winner=int(sizes.argmax())
    if sizes[winner]<sizes.sum()*minimum_share:return selected
    candidate=selected[ids[selected]==winner];points=vertices[np.unique(triangles[candidate])]
    low,high=points.min(0),points.max(0)
    hips=np.asarray(anchors['Hips']);neck=np.asarray(anchors['Neck']);chest=np.asarray(anchors['Spine3'])
    width=max(abs(np.asarray(anchors[side+'Arm'])[0]-hips[0]) for side in ['Left','Right'])
    complete=(low[1]<hips[1]+.03 and high[1]>neck[1]-.035 and
              low[0]<hips[0]-width*.75 and high[0]>hips[0]+width*.75 and
              low[2]<min(hips[2],chest[2])-.04 and high[2]>max(hips[2],chest[2])+.025)
    return candidate if complete else selected


def fnv_path(path):
    value=14695981039346656037
    for byte in path.lower().replace('/','\\').encode('utf-8'):
        value=((value^byte)*1099511628211)&0xffffffffffffffff
    return str(value)


def covered_regions(fractions,source):
    """Never hide a whole section merely because most of it is covered."""
    regions=[region for region,fraction in fractions.items() if fraction>=.995]
    source=source.replace('\\','/').lower()
    if '/legs/' in source or {'hips','lleg_0','rleg_0'}.issubset(regions):regions.append('pelvis')
    return safe_hidden_regions(regions,source)


def safe_hidden_regions(regions,source,compact_pelvis=False):
    source=source.replace('\\','/').lower()
    outer=bool(re.search(r'(?:^|/)t2_',source))
    # Outer-jacket variants can be open at the front. Proximity to their back
    # or side panels does not prove that the exposed abdomen is covered.
    return [region for region in regions
            if not (outer and (region.startswith('torso_') or region=='hips'))
            and not (compact_pelvis and region=='pelvis')]


def cloth_open_edges(triangles):
    vertices,ids=np.unique(np.round(triangles.reshape(-1,3),3),axis=0,return_inverse=True)
    tri=ids.reshape(-1,3)
    edges=np.sort(np.concatenate([tri[:,[0,1]],tri[:,[1,2]],tri[:,[2,0]]]),axis=1)
    edges,count=np.unique(edges,axis=0,return_counts=True)
    return vertices[edges[count==1]]


class ClothingCoverage:
    """Measure skin sections against a garment in the same native rest pose."""
    def __init__(self,parts,target_world):
        import cp77 as c
        self.target_world=target_world;self.sections={};self.section_vertices={}
        for part in ['body','body_fpp','arms_fpp']:
            if part not in parts:continue
            doc,data=c.load_glb(parts[part])
            for mesh in doc['meshes']:
                region=mesh.get('extras',{}).get('avatarbridge_region','keep')
                if region in ['keep','pelvis']:continue
                for p in mesh['primitives']:
                    vertices=b.accessor_array(doc,data,p['attributes']['POSITION'])
                    tri=vertices[b.accessor_array(doc,data,p['indices']).reshape(-1,3).astype(int)]
                    cross=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);area=np.linalg.norm(cross,axis=1)
                    normals=cross/np.maximum(area[:,None],1e-10)
                    self.sections.setdefault(region,[]).append((tri.mean(1),normals,area))
                    self.section_vertices.setdefault(region,[]).append(tri.reshape(-1,3))
        self.sections={key:tuple(np.concatenate([row[i] for row in rows]) for i in range(3)) for key,rows in self.sections.items()}
        self.section_vertices={key:np.concatenate(rows) for key,rows in self.section_vertices.items()}

    def closed_leg(self,triangles,region,boundary=None):
        """Mask a leg section only when actual fabric closes every tested ring.

        Intersect the real triangles, without a convex hull that could bridge
        a slit. Check both boundaries and interior planes, with overlap at a
        cuff. No coverage is inferred for the torso or an open jacket.
        """
        if not region.startswith(('lleg_','rleg_')) or not hasattr(self,'section_vertices'):return False
        vertices=self.section_vertices.get(region)
        if vertices is None or not len(vertices):return False
        side='Left' if region.startswith('lleg') else 'Right'
        names=[side+n for n in ['UpLeg','Leg','Foot']]
        if any(name not in self.target_world for name in names):return False
        curve=np.array([self.target_world[name][:3,3] for name in names]);curve=curve[curve[:,1].argsort()]
        low,high=float(vertices[:,1].min()),float(vertices[:,1].max())
        if high-low<.005:return False
        if boundary is None:boundary=cloth_open_edges(triangles)
        if len(boundary):
            edge_low=boundary[:,:,1].min(1);edge_high=boundary[:,:,1].max(1)
            if ((edge_low<high+.008)&(edge_high>low-.008)).any():return False
        directions=np.column_stack([np.cos(np.arange(192)*np.pi/96),np.sin(np.arange(192)*np.pi/96)])
        tlo=triangles[:,:,1].min(1);thi=triangles[:,:,1].max(1)
        for height in np.linspace(low-.008,high+.008,11):
            center=np.array([np.interp(height,curve[:,1],curve[:,i]) for i in [0,2]])
            crossing=triangles[(tlo<height)&(thi>height)]
            if not len(crossing):return False
            # Every non-degenerate slice of a triangle contributes one segment.
            points=np.full((len(crossing),3,2),np.nan)
            for col,(i,j) in enumerate([(0,1),(1,2),(2,0)]):
                a,z=crossing[:,i],crossing[:,j];delta=z-a
                valid=((a[:,1]-height)*(z[:,1]-height)<=0)&(abs(delta[:,1])>1e-8)
                points[valid,col]=(a[valid]+delta[valid]*((height-a[valid,1])/delta[valid,1])[:,None])[:,[0,2]]
            segments=[]
            for row in points:
                valid=row[np.isfinite(row).all(1)]
                if len(valid)>=2:segments.append(valid[:2])
            if not segments:return False
            segments=np.asarray(segments)-center
            a=segments[:,0];edge=segments[:,1]-a
            det=directions[:,0,None]*(-edge[None,:,1])-directions[:,1,None]*(-edge[None,:,0])
            inv=np.divide(1.,det,out=np.zeros_like(det),where=abs(det)>1e-9)
            distance=(a[None,:,0]*(-edge[None,:,1])-a[None,:,1]*(-edge[None,:,0]))*inv
            along=(directions[:,0,None]*a[None,:,1]-directions[:,1,None]*a[None,:,0])*inv
            # Ignore unrelated distant fabric while allowing loose trousers.
            # The boundary check above rejects slits even if the other leg
            # would happen to intersect a ray through the opening.
            near=vertices[abs(vertices[:,1]-np.clip(height,low,high))<max((high-low)/10,.015)]
            if not len(near):near=vertices
            radius=float(np.linalg.norm(near[:,[0,2]]-center,axis=1).max())
            limit=max(radius*2+.080,.25)
            hits=((distance>.005)&(distance<limit)&(along>=-1e-7)&(along<=1+1e-7)&(abs(det)>1e-9)).any(1)
            if not hits.all():return False
        return True

    def garment(self,path,source):
        import cp77 as c
        source=source.replace('\\','/').lower()
        if 'shadow' in source or '/feet/' in source:return {'regions':[],'fractions':{}}
        doc,data=c.load_glb(path);transforms=pose_transforms(doc,data,self.target_world);triangles=[]
        for mesh in doc['meshes']:
            for p in mesh['primitives']:
                v=b.accessor_array(doc,data,p['attributes']['POSITION'])
                v=posed(v,native_weights(doc,data,p['attributes']),transforms)[0]
                triangles.append(v[b.accessor_array(doc,data,p['indices']).reshape(-1,3).astype(int)])
        primary=max(triangles,key=len)
        triangles=np.concatenate(triangles);tree=cKDTree(triangles.mean(1))
        cross=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
        normal=cross/np.maximum(np.linalg.norm(cross,axis=1,keepdims=True),1e-10)
        fractions={};regions=[]
        for region,(points,normals,area) in self.sections.items():
            if region.startswith(('lleg','rleg')) and '/legs/' not in source and 't2_' not in source:continue
            if region.startswith(('larm','rarm','torso')) and '/legs/' in source:continue
            _,near=tree.query(points,k=min(16,len(triangles)))
            if near.ndim==1:near=near[:,None]
            closest,k=nearest_triangle(points,triangles[near]);chosen=near[np.arange(len(points)),k]
            delta=points-closest;distance=np.linalg.norm(delta,axis=1)
            signed=(delta*normal[chosen]).sum(1)
            tangent=np.sqrt(np.maximum(distance**2-signed**2,0))
            # Interior projections cover the skin; points past a neckline or
            # cuff land on an edge and must not hide an exposed section.
            covered=(distance<.115)&(tangent<.024+abs(signed)*.35)&(abs((normals*normal[chosen]).sum(1))>.20)
            fraction=float(area[covered].sum()/max(area.sum(),1e-10));fractions[region]=fraction
        regions=covered_regions(fractions,source)
        if '/legs/' in source:
            boundary=cloth_open_edges(primary)
            for region in fractions:
                if region not in regions and self.closed_leg(primary,region,boundary):regions.append(region)
        return {'regions':regions,'fractions':fractions,
                'chunk_count':sum(len(m['primitives']) for m in doc['meshes'])}


def group(name):
    n=name.lower().removeprefix('shadow_')
    side='l' if n.startswith(('left','l_')) else 'r' if n.startswith(('right','r_')) else ''
    if side and any(s in n for s in ['arm','shoulder','elbow','wrist','hand','finger','thumb','biceps','triceps','deltoid','knuckle','pinkybase','shl_']):return side+'arm'
    if side and any(s in n for s in ['leg','thigh','calf','foot','toe','knee']):return side+'leg'
    if any(s in n for s in ['head','neck','face','eye','jaw','sternocleidomastoid','jali','cheek','brow','nose','lid_']):return 'head'
    return 'torso'


def vertex_groups(doc,data,primitive):
    a=primitive['attributes']
    if not doc.get('skins') or 'JOINTS_0' not in a:return np.full(doc['accessors'][a['POSITION']]['count'],'torso')
    joints=b.accessor_array(doc,data,a['JOINTS_0']).astype(int)
    weights=b.accessor_array(doc,data,a['WEIGHTS_0'])
    names=[doc['nodes'][j].get('name','') for j in doc['skins'][0]['joints']]
    labels=np.array([group(n) for n in names])
    return labels[joints[np.arange(len(joints)),weights.argmax(axis=1)]]


def native_weights(doc,data,attributes,used=None):
    count=doc['accessors'][attributes['POSITION']]['count'] if used is None else len(used)
    dense=np.zeros((count,len(doc.get('skins',[{}])[0].get('joints',[]))))
    for channel in [0,1]:
        jkey,wkey=f'JOINTS_{channel}',f'WEIGHTS_{channel}'
        if jkey not in attributes:continue
        joints=b.accessor_array(doc,data,attributes[jkey]).astype(int)
        weights=b.accessor_array(doc,data,attributes[wkey])
        if used is not None:joints,weights=joints[used],weights[used]
        for col in range(4):np.add.at(dense,(np.arange(count),joints[:,col]),weights[:,col])
    return dense


def pose_transforms(doc,data,targets):
    """Garment bind poses differ from the player's rest pose, even for V.

    Normalize only for geometric fitting; the fitted result is transformed
    back before import so native muscle and cloth animation bindings survive.
    """
    import cp77 as c
    if not doc.get('skins'):return np.empty((0,4,4))
    skin=doc['skins'][0];names=[doc['nodes'][i].get('name','') for i in skin['joints']]
    if not targets:return np.tile(np.eye(4),(len(names),1,1))
    if 'inverseBindMatrices' in skin:
        inverse=b.accessor_array(doc,data,skin['inverseBindMatrices']).reshape(-1,4,4).transpose(0,2,1)
        source=np.linalg.inv(inverse)
    else:source=np.array([c.world_matrices(doc)[i] for i in skin['joints']])
    target_names=[n.removeprefix('shadow_') for n in names]
    known=[i for i,n in enumerate(target_names) if n in targets]
    if not known:raise ValueError('Il rig del capo non condivide ossa con il corpo del gioco.')
    transforms=np.empty_like(source)
    for i in known:transforms[i]=targets[target_names[i]]@np.linalg.inv(source[i])
    for i,n in enumerate(names):
        if i in known:continue
        # A garment-specific dangle bone keeps its offset from the nearest
        # compatible native anchor; it is never merged into that anchor.
        anchor=min(known,key=lambda k:np.linalg.norm(source[k,:3,3]-source[i,:3,3]))
        transforms[i]=transforms[anchor]
    return transforms


def posed(positions,weights,transforms):
    if not weights.shape[1]:return positions.copy(),np.tile(np.eye(4),(len(positions),1,1))
    total=weights.sum(axis=1,keepdims=True)
    normalized=np.divide(weights,total,out=np.zeros_like(weights),where=total>1e-8)
    matrices=np.einsum('nj,jab->nab',normalized,transforms)
    matrices[total[:,0]<=1e-8]=np.eye(4)
    return np.einsum('nij,nj->ni',matrices[:,:3,:3],positions)+matrices[:,:3,3],matrices


def nearest_triangle(points,triangles):
    """Closest point for N points and K candidate triangles, including edges."""
    a,z,c=triangles[:,:,0],triangles[:,:,1],triangles[:,:,2]
    p=points[:,None,:];u=z-a;v=c-a;w=p-a
    uu=(u*u).sum(axis=2);uv=(u*v).sum(axis=2);vv=(v*v).sum(axis=2)
    wu=(w*u).sum(axis=2);wv=(w*v).sum(axis=2)
    det=np.maximum(uu*vv-uv*uv,1e-16)
    s=(wu*vv-wv*uv)/det;t=(wv*uu-wu*uv)/det
    plane=a+s[:,:,None]*u+t[:,:,None]*v
    inside=(s>=0)&(t>=0)&(s+t<=1)
    choices=[plane];valid=[inside]
    for x,y in [(a,z),(z,c),(c,a)]:
        edge=y-x
        alpha=np.clip(((p-x)*edge).sum(axis=2)/np.maximum((edge*edge).sum(axis=2),1e-16),0,1)
        choices.append(x+alpha[:,:,None]*edge);valid.append(np.ones_like(inside))
    candidates=np.stack(choices,axis=2)
    distance=((candidates-p[:,:,None,:])**2).sum(axis=3)
    distance=np.where(np.stack(valid,axis=2),distance,np.inf)
    flat=distance.reshape(len(points),-1).argmin(axis=1)
    k=flat//4;which=flat%4
    return candidates[np.arange(len(points)),k,which],k


def seam_relaxer(positions,triangles,amount=8.):
    """Smooth a displacement field, welding UV seams without shrinking the mesh."""
    _,remap=np.unique(np.round(positions,5),axis=0,return_inverse=True)
    count=np.bincount(remap);size=len(count)
    edges=np.unique(np.sort(remap[np.concatenate([triangles[:,[0,1]],triangles[:,[1,2]],triangles[:,[2,0]]])],axis=1),axis=0)
    edges=edges[edges[:,0]!=edges[:,1]]
    rows=np.r_[edges[:,0],edges[:,1]];cols=np.r_[edges[:,1],edges[:,0]]
    adjacency=coo_matrix((np.ones(len(rows)),(rows,cols)),shape=(size,size)).tocsr()
    laplacian=diags(np.asarray(adjacency.sum(1)).ravel())-adjacency
    solve=factorized((eye(size,format='csc')+amount*laplacian).tocsc())
    def relax(values):
        mean=np.zeros((size,values.shape[1]));np.add.at(mean,remap,values)
        return solve(mean/count[:,None])[remap]
    return relax


def conservative_radii(radii):
    """Smooth the cage without averaging away a knee or a skin protrusion."""
    radii=np.asarray(radii)
    expanded=maximum_filter(radii,size=(3,3),mode=('nearest','wrap'))
    return np.maximum(radii,gaussian_filter(expanded,(1,1),mode=('nearest','wrap')))


def follow_body_weights(positions,labels,dense,doc,envelope):
    """Close fabric follows the skin; distant cloth panels keep their rig."""
    if not dense.shape[1] or not hasattr(envelope,'skin_weights'):return dense
    names=[doc['nodes'][i].get('name','').removeprefix('shadow_') for i in doc['skins'][0]['joints']]
    lookup={name:i for i,name in enumerate(names)}
    result=dense.copy()
    for label in np.unique(labels):
        if label not in ['torso','larm','rarm','lleg','rleg']:continue
        selected=np.flatnonzero(labels==label);points=positions[selected]
        closest,_=envelope.nearest(points,label)
        distance=np.linalg.norm(points-closest,axis=1)
        influence=np.clip((.080-distance)/.055,0,1)*.85
        if not (influence>0).any():continue
        joints,weights=envelope.weights(points,np.full(len(points),label))
        skin=np.zeros((len(points),dense.shape[1]))
        for column in range(joints.shape[1]):
            target=np.array([lookup.get(envelope.joint_names[i],-1) for i in joints[:,column]])
            valid=target>=0
            np.add.at(skin,(np.flatnonzero(valid),target[valid]),weights[valid,column])
        total=skin.sum(1)
        # Unknown anchors must never be silently transferred to another bone.
        influence[total<.999]=0
        skin/=np.maximum(total[:,None],1e-8)
        result[selected]=dense[selected]*(1-influence[:,None])+skin*influence[:,None]
    return result


def limited_weights(dense,count):
    if not dense.shape[1]:return dense
    joints=np.argsort(-dense,axis=1)[:,:min(count,dense.shape[1])]
    result=np.zeros_like(dense)
    np.put_along_axis(result,joints,np.take_along_axis(dense,joints,axis=1),axis=1)
    return result/np.maximum(result.sum(1,keepdims=True),1e-8)


class LowerBodyCage:
    """Continuous cross-section envelope: avoid snapping shorts across a thigh.

    Convex slices bridge small skin folds; the two leg cages blend into the
    pelvis. This is a rest-pose fit, not a cloth simulation.
    """
    def __init__(self,envelope):
        joints={n['name']:np.array(n['translation']) for n in envelope.joint_nodes}
        self.hips_x=float(joints['Hips'][0])
        self.left_sign=1 if joints['LeftUpLeg'][0]>joints['RightUpLeg'][0] else -1
        self.hips=float(joints['Hips'][1]);self.angles=np.linspace(-np.pi,np.pi,96,endpoint=False)
        vertices=getattr(envelope,'cage_vertices',envelope.vertices)
        indices=getattr(envelope,'cage_triangles',envelope.triangles)
        self.floor=float(vertices[:,1].min())-.012
        self.foot_height=float(max(joints['LeftFoot'][1],joints['RightFoot'][1]))
        self.heights=np.linspace(self.floor-.01,self.hips+.23,100)
        directions=np.column_stack([np.cos(self.angles),np.sin(self.angles)])
        triangles=vertices[indices]
        low=triangles[:,:,1].min(1);high=triangles[:,:,1].max(1)
        slices=[]
        for height in self.heights:
            crossing=triangles[(low<=height)&(high>=height)];points=[]
            for i,j in [(0,1),(1,2),(2,0)]:
                a,z=crossing[:,i],crossing[:,j];delta=z-a
                valid=((a[:,1]-height)*(z[:,1]-height)<=0)&(abs(delta[:,1])>1e-8)
                a,delta=a[valid],delta[valid]
                points.append((a+delta*((height-a[:,1])/delta[:,1])[:,None])[:,[0,2]])
            slices.append(np.concatenate(points))
        self.fields={}
        for label,sign in [('torso',0),('lleg',self.left_sign),('rleg',-self.left_sign)]:
            if sign:
                side='Left' if label=='lleg' else 'Right'
                samples=np.array([joints[side+n] for n in ['UpLeg','Leg','Foot']]);samples=samples[samples[:,1].argsort()]
                centers=np.column_stack([np.interp(self.heights,samples[:,1],samples[:,i]) for i in [0,2]])
            else:centers=np.tile(joints['Hips'][[0,2]],(len(self.heights),1))
            radii=[]
            for row,points in enumerate(slices):
                if sign:points=points[(points[:,0]-self.hips_x)*sign>=-.015]
                if len(points)<4:radii.append(np.full(len(self.angles),.04));continue
                try:polygon=points[ConvexHull(points).vertices]-centers[row]
                except QhullError:radii.append(np.full(len(self.angles),.04));continue
                edge=np.roll(polygon,-1,axis=0)-polygon
                det=directions[:,0,None]*(-edge[None,:,1])-directions[:,1,None]*(-edge[None,:,0])
                inv=np.divide(1.,det,out=np.zeros_like(det),where=abs(det)>1e-8)
                distance=(polygon[None,:,0]*(-edge[None,:,1])-polygon[None,:,1]*(-edge[None,:,0]))*inv
                along=(directions[:,0,None]*polygon[None,:,1]-directions[:,1,None]*polygon[None,:,0])*inv
                radii.append(np.where((distance>=0)&(along>=0)&(along<=1)&(abs(det)>1e-8),distance,0).max(1))
            self.fields[label]=(centers,conservative_radii(radii))

    def project(self,positions,clearance,footwear=False,fit=False,ease=.012):
        outputs={};height=positions[:,1]
        row=np.clip((height-self.heights[0])/(self.heights[-1]-self.heights[0])*99,0,99)
        iy=np.minimum(row.astype(int),98);fy=row-iy
        for label,(centers,radii) in self.fields.items():
            if footwear and label!='torso':
                # A sole must cover the paw's footprint, including toes whose
                # widest section lies above the sole. Preserve the ankle taper.
                footprint=radii[(self.heights>=self.floor+.02)&(self.heights<=self.foot_height+.04)].max(axis=0)
                sole=np.clip((self.foot_height-self.heights)/.10,0,1)
                radii=np.maximum(radii,footprint[None,:]*sole[:,None])
            center=np.column_stack([np.interp(height,self.heights,centers[:,i]) for i in range(2)])
            delta=positions[:,[0,2]]-center;length=np.linalg.norm(delta,axis=1)
            angle=(np.arctan2(delta[:,1],delta[:,0])+np.pi)/(2*np.pi)*96
            ia=np.floor(angle).astype(int)%96;fraction=angle-np.floor(angle)
            radius=(radii[iy,ia]*(1-fraction)+radii[iy,(ia+1)%96]*fraction)*(1-fy)
            radius+=(radii[iy+1,ia]*(1-fraction)+radii[iy+1,(ia+1)%96]*fraction)*fy
            wanted=np.maximum(length,radius+clearance)
            if fit:
                near=length-radius<.18
                wanted=np.where(near,np.clip(length,radius+clearance,radius+clearance+ease),wanted)
            scale=wanted/np.maximum(length,1e-6)
            out=positions.copy();out[:,[0,2]]=center+delta*scale[:,None];outputs[label]=out
        side=np.clip(((positions[:,0]-self.hips_x)*self.left_sign+.025)/.05,0,1)
        legs=outputs['lleg']*side[:,None]+outputs['rleg']*(1-side[:,None])
        blend=np.clip((height-(self.hips-.17))/.15,0,1);blend=blend*blend*(3-2*blend)
        result=legs*(1-blend[:,None])+outputs['torso']*blend[:,None]
        outside=(height<self.heights[0])|(height>self.heights[-1])
        result[outside]=positions[outside]
        return result


class RadialCage:
    """Grow a garment around a continuous torso or arm cross section.

    Nearest-surface snapping from inside a limb can send adjacent vertices
    to opposite sides. A radial field preserves their side of the limb.
    """
    def __init__(self,envelope,label):
        joints={n['name']:np.array(n['translation']) for n in envelope.joint_nodes}
        if label=='torso':
            self.origin=np.zeros(3);self.basis=np.eye(3)
            curve=np.array([joints[n] for n in ['Hips','Spine1','Spine3','Neck']])
        else:
            side='Left' if label=='larm' else 'Right'
            curve=np.array([joints[side+n] for n in ['Arm','ForeArm','Hand']])
            self.origin=curve[0]
            axis=curve[-1]-curve[0];axis/=np.linalg.norm(axis)
            u=np.array([0.,0.,1.]);u-=axis*(u@axis);u/=np.linalg.norm(u)
            self.basis=np.column_stack([u,axis,np.cross(u,axis)])
        curve=(curve-self.origin)@self.basis;curve=curve[curve[:,1].argsort()]
        self.heights=np.linspace(curve[0,1]-.025,curve[-1,1]+.035,100)
        self.centers=np.column_stack([np.interp(self.heights,curve[:,1],curve[:,i]) for i in [0,2]])
        vertices=getattr(envelope,'volume_vertices',envelope.vertices)
        indices=getattr(envelope,'volume_triangles',envelope.triangles)
        groups=getattr(envelope,'volume_groups',envelope.groups)
        ids=groups.get(label,groups['all'])[0]
        triangles=((vertices-self.origin)@self.basis)[indices[ids]]
        low=triangles[:,:,1].min(1);high=triangles[:,:,1].max(1)
        angles=np.linspace(-np.pi,np.pi,96,endpoint=False)
        directions=np.column_stack([np.cos(angles),np.sin(angles)]);radii=[]
        for row,height in enumerate(self.heights):
            crossing=triangles[(low<=height)&(high>=height)];points=[]
            for i,j in [(0,1),(1,2),(2,0)]:
                a,z=crossing[:,i],crossing[:,j];delta=z-a
                valid=((a[:,1]-height)*(z[:,1]-height)<=0)&(abs(delta[:,1])>1e-8)
                a,delta=a[valid],delta[valid]
                points.append((a+delta*((height-a[:,1])/delta[:,1])[:,None])[:,[0,2]])
            points=np.concatenate(points)
            if len(points)<4:radii.append(np.full(96,0. if label=='torso' else .035));continue
            try:polygon=points[ConvexHull(points).vertices]-self.centers[row]
            except QhullError:radii.append(np.full(96,0. if label=='torso' else .035));continue
            edge=np.roll(polygon,-1,axis=0)-polygon
            det=directions[:,0,None]*(-edge[None,:,1])-directions[:,1,None]*(-edge[None,:,0])
            inv=np.divide(1.,det,out=np.zeros_like(det),where=abs(det)>1e-8)
            distance=(polygon[None,:,0]*(-edge[None,:,1])-polygon[None,:,1]*(-edge[None,:,0]))*inv
            along=(directions[:,0,None]*polygon[None,:,1]-directions[:,1,None]*polygon[None,:,0])*inv
            radii.append(np.where((distance>=0)&(along>=0)&(along<=1)&(abs(det)>1e-8),distance,0).max(1))
        radii=np.asarray(radii)
        if label=='torso':
            # UV-separated skin may leave only the front of an upper slice.
            # A missing posterior ray must not collapse Garment Support
            # through the back. Interpolate bounded gaps in actual measured
            # posterior radii, without extrapolating collar or hem openings.
            for column in np.flatnonzero(np.sin(angles)>.05):
                valid=np.flatnonzero(radii[:,column]>.005)
                for row in np.flatnonzero(radii[:,column]<=.005):
                    below=valid[valid<row];above=valid[valid>row]
                    if not len(below) or not len(above):continue
                    first,last=below[-1],above[0]
                    if self.heights[last]-self.heights[first]>.30:continue
                    radii[row,column]=np.interp(self.heights[row],self.heights[[first,last]],radii[[first,last],column])
        if label=='torso':radii=np.where(radii>0,radii,.035)
        self.radii=conservative_radii(radii)

    def project(self,positions,clearance,fit=False,ease=.010):
        local=(positions-self.origin)@self.basis;height=local[:,1]
        center=np.column_stack([np.interp(height,self.heights,self.centers[:,i]) for i in range(2)])
        delta=local[:,[0,2]]-center;length=np.linalg.norm(delta,axis=1)
        angle=(np.arctan2(delta[:,1],delta[:,0])+np.pi)/(2*np.pi)*96
        ia=np.floor(angle).astype(int)%96;fraction=angle-np.floor(angle)
        row=np.clip((height-self.heights[0])/(self.heights[-1]-self.heights[0])*99,0,99)
        iy=np.minimum(row.astype(int),98);fy=row-iy;r=self.radii
        radius=(r[iy,ia]*(1-fraction)+r[iy,(ia+1)%96]*fraction)*(1-fy)
        radius+=(r[iy+1,ia]*(1-fraction)+r[iy+1,(ia+1)%96]*fraction)*fy
        wanted=np.maximum(length,radius+clearance)
        if fit:
            near=length-radius<.18
            wanted=np.where(near,np.clip(length,radius+clearance,radius+clearance+ease),wanted)
        scale=wanted/np.maximum(length,1e-6)
        result=local.copy();result[:,[0,2]]=center+delta*scale[:,None]
        result=result@self.basis.T+self.origin
        outside=(height<self.heights[0])|(height>self.heights[-1])
        result[outside]=positions[outside]
        return result


class Envelope:
    def __init__(self,parts,rig=None):
        import cp77 as c
        vertices=[];normals=[];triangles=[];labels=[];skin_weights=[];offset=0
        cage_vertices=[];cage_triangles=[];cage_offset=0
        volume_vertices=[];volume_triangles=[];volume_labels=[];volume_uv=[];volume_offset=0
        for part in ['body','head']:
            doc,data=c.load_glb(parts[part]);mats=doc.get('materials',[])
            skin=doc['skins'][0];names=[doc['nodes'][j]['name'] for j in skin['joints']]
            if not hasattr(self,'joint_names'):
                self.joint_names=names
                self.joint_nodes=[copy.deepcopy(doc['nodes'][j]) for j in skin['joints']]
                self.inverse=b.accessor_array(doc,data,skin['inverseBindMatrices'])
            intact=bool(doc.get('extras',{}).get('avatarbridge_intact_skin'))
            all_primitives=[p for m in doc['meshes'] if intact or m.get('extras',{}).get('avatarbridge_coverage')!='pelvis' for p in m['primitives']]
            chosen=[p for p in all_primitives if any(n in mats[p.get('material',0)].get('name','').lower() for n in ['body','skin'])]
            if not chosen:
                chosen=[max(all_primitives,key=lambda p:doc['accessors'][p['indices']]['count'])]
            if part=='body':
                for primitive in all_primitives:
                    material=mats[primitive.get('material',0)].get('name','').lower()
                    if any(word in material for word in ['feather','hair','eyelens']):continue
                    v=b.accessor_array(doc,data,primitive['attributes']['POSITION'])
                    tri=b.accessor_array(doc,data,primitive['indices']).reshape(-1,3).astype(int)
                    used,indices=np.unique(tri,return_inverse=True)
                    volume_vertices.append(v[used]);volume_triangles.append(indices.reshape(-1,3)+volume_offset)
                    volume_uv.append(b.accessor_array(doc,data,primitive['attributes']['TEXCOORD_0'])[used])
                    volume_labels.extend(vertex_groups(doc,data,primitive)[used]);volume_offset+=len(used)
                # Footwear must cover pads/claws too. Long secondary fur on
                # the back is excluded from the trousers' collision envelope.
                foot_cap=max(n['translation'][1] for n in self.joint_nodes if n['name'] in ['LeftFoot','RightFoot'])+.15
                for primitive in all_primitives:
                    v=b.accessor_array(doc,data,primitive['attributes']['POSITION'])
                    tri=b.accessor_array(doc,data,primitive['indices']).reshape(-1,3).astype(int)
                    if not any(primitive is p for p in chosen):tri=tri[(v[tri,1]<foot_cap).all(axis=1)]
                    if not len(tri):continue
                    used,indices=np.unique(tri,return_inverse=True)
                    cage_vertices.append(v[used]);cage_triangles.append(indices.reshape(-1,3)+cage_offset);cage_offset+=len(used)
            for p in chosen:
                a=p['attributes'];v=b.accessor_array(doc,data,a['POSITION']);n=b.accessor_array(doc,data,a['NORMAL'])
                tri=b.accessor_array(doc,data,p['indices']).reshape(-1,3).astype(int)
                used,indices=np.unique(tri,return_inverse=True)
                vertices.append(v[used]);normals.append(n[used]);triangles.append(indices.reshape(-1,3)+offset)
                joints=b.accessor_array(doc,data,a['JOINTS_0'])[used].astype(int)
                weights=b.accessor_array(doc,data,a['WEIGHTS_0'])[used]
                dense=np.zeros((len(used),len(self.joint_names)),dtype=np.float32)
                remap=np.array([self.joint_names.index(name) for name in names])
                for col in range(4):np.add.at(dense,(np.arange(len(used)),remap[joints[:,col]]),weights[:,col])
                skin_weights.append(dense)
                labels.extend(vertex_groups(doc,data,p)[used]);offset+=len(used)
        self.vertices=np.concatenate(vertices);self.normals=np.concatenate(normals);self.triangles=np.concatenate(triangles)
        self.skin_weights=np.concatenate(skin_weights)
        self.target_world=c.rig_matrices(rig) if rig else {n['name']:b.node_matrix(n) for n in self.joint_nodes}
        self.cage_vertices=np.concatenate(cage_vertices);self.cage_triangles=np.concatenate(cage_triangles)
        self.volume_vertices=np.concatenate(volume_vertices) if volume_vertices else self.vertices
        self.volume_triangles=np.concatenate(volume_triangles) if volume_triangles else self.triangles
        volume_labels=np.array(volume_labels) if volume_labels else np.array(labels)
        self.volume_groups={}
        for key in ['torso','larm','rarm','all']:
            mask=np.ones(len(self.volume_triangles),dtype=bool) if key=='all' else (volume_labels[self.volume_triangles]==key).any(axis=1)
            ids=np.flatnonzero(mask)
            if len(ids):self.volume_groups[key]=(ids,None)
        if volume_uv and 'torso' in self.volume_groups:
            ids=self.volume_groups['torso'][0]
            anchors={n['name']:np.asarray(n['translation']) for n in self.joint_nodes}
            self.volume_groups['torso']=(torso_skin_surface(self.volume_vertices,self.volume_triangles,np.concatenate(volume_uv),ids,anchors),None)
        labels=np.array(labels)
        self.groups={}
        for key in ['head','torso','larm','rarm','lleg','rleg','all']:
            mask=np.ones(len(self.triangles),dtype=bool) if key=='all' else (labels[self.triangles]==key).any(axis=1)
            ids=np.flatnonzero(mask)
            if len(ids):self.groups[key]=(ids,cKDTree(self.vertices[self.triangles[ids]].mean(axis=1)))

    def nearest(self,points,label):
        ids,tree=self.groups.get(label,self.groups['all'])
        _,near=tree.query(points,k=min(12,len(ids)))
        if near.ndim==1:near=near[:,None]
        tri_ids=ids[near];tri=self.vertices[self.triangles[tri_ids]]
        closest,k=nearest_triangle(points,tri)
        chosen=self.triangles[tri_ids[np.arange(len(points)),k]]
        return closest,chosen

    def weights(self,positions,labels):
        dense=np.zeros((len(positions),len(self.joint_names)),dtype=np.float32)
        for label in np.unique(labels):
            selected=np.flatnonzero(labels==label)
            closest,chosen=self.nearest(positions[selected],label)
            a,z,c=self.vertices[chosen].transpose(1,0,2)
            u=z-a;v=c-a;w=closest-a
            uu=(u*u).sum(1);uv=(u*v).sum(1);vv=(v*v).sum(1)
            wu=(w*u).sum(1);wv=(w*v).sum(1);det=np.maximum(uu*vv-uv*uv,1e-16)
            s=(wu*vv-wv*uv)/det;t=(wv*uu-wu*uv)/det
            bary=np.clip(np.column_stack([1-s-t,s,t]),0,1);bary/=np.maximum(bary.sum(1,keepdims=True),1e-8)
            dense[selected]=(self.skin_weights[chosen]*bary[:,:,None]).sum(1)
        joints=np.argsort(-dense,axis=1)[:,:4];weights=np.take_along_axis(dense,joints,axis=1)
        weights/=np.maximum(weights.sum(1,keepdims=True),1e-8)
        return joints,weights

    def project(self,positions,labels,clearance,max_distance=.5):
        result=positions.copy();moved=0;maximum=0.
        for label in np.unique(labels):
            selected=np.flatnonzero(labels==label);p=positions[selected]
            closest,chosen=self.nearest(p,label)
            n=self.normals[chosen].mean(axis=1);n/=np.maximum(np.linalg.norm(n,axis=1,keepdims=True),1e-10)
            signed=((p-closest)*n).sum(axis=1)
            touch=signed<clearance
            # Local unskinned attachments are not in the body's rest space.
            touch&=np.linalg.norm(p-closest,axis=1)<max_distance
            target=closest+n*clearance
            result[selected[touch]]=target[touch]
            moved+=int(touch.sum())
            if touch.any():maximum=max(maximum,float(np.linalg.norm(target[touch]-p[touch],axis=1).max()))
        return result,{'moved_vertices':moved,'max_displacement':maximum}

    def garment_volume(self,positions,labels,clearance,fit=True):
        if not hasattr(self,'volume_cages'):self.volume_cages={}
        result=positions.copy()
        for label in np.unique(labels):
            selected=np.flatnonzero(labels==label);points=positions[selected]
            if label in ['torso','larm','rarm']:
                if label not in self.volume_cages:self.volume_cages[label]=RadialCage(self,label)
                target=self.volume_cages[label].project(points,clearance,fit,ease=.018 if clearance>.01 else .010)
                if label=='torso':
                    if not hasattr(self,'lower_body_cage'):self.lower_body_cage=LowerBodyCage(self)
                    lower=self.lower_body_cage.project(points,clearance,fit=fit)
                    hips=next(n['translation'][1] for n in self.joint_nodes if n['name']=='Hips')
                    blend=np.clip((points[:,1]-(hips+.02))/.08,0,1);blend=blend*blend*(3-2*blend)
                    target=lower*(1-blend[:,None])+target*blend[:,None]
                result[selected]=target
            elif label in ['lleg','rleg']:
                if not hasattr(self,'lower_body_cage'):self.lower_body_cage=LowerBodyCage(self)
                result[selected]=self.lower_body_cage.project(points,clearance,fit=fit)
            else:result[selected]=self.project(points,np.full(len(points),label),clearance)[0]
        distance=np.linalg.norm(result-positions,axis=1)
        return result,{'moved_vertices':int((distance>1e-6).sum()),'max_displacement':float(distance.max(initial=0))}


    def layered_hips(self,positions,labels):
        """Grow an untucked upper layer beyond trousers at the waist/seat.

        Trousers use25mm clearance and up to18mm near-skin ease. Reserve
        another17mm for the upper layer, fading above the waist and below the
        seat. Distant hems and the opening of a coat retain their shape.
        """
        if not hasattr(self,'lower_body_cage'):self.lower_body_cage=LowerBodyCage(self)
        hips=next(n['translation'][1] for n in self.joint_nodes if n['name']=='Hips')
        below=np.clip((positions[:,1]-(hips-.42))/.12,0,1)
        above=np.clip((hips+.32-positions[:,1])/.12,0,1)
        # Jacket tails often use thigh/leg bones. They are still the upper
        # garment and must clear trousers just like its spine-bound panels.
        blend=below*above*np.isin(labels,['torso','lleg','rleg']);blend=blend*blend*(3-2*blend)
        result=positions.copy();selected=np.flatnonzero(blend>0)
        if len(selected):
            target=self.lower_body_cage.project(positions[selected],.060,fit=False)
            result[selected]+=(target-positions[selected])*blend[selected,None]
        return result


def stitch_fitted_seams(writer,records,transforms,envelope,clearance,top_role=None):
    """Keep source-coincident cloth vertices together across UV/material seams.

    Preserve separate UVs and topology. Share posed seam positions, skin
    weights and support states, then rebuild each native inverse binding.
    """
    import cp77 as c
    if not records:return 0
    doc=writer.doc;data=bytes(writer.bin);rows=[]
    reference=np.concatenate([record['reference'] for record in records])
    _,groups=np.unique(np.round(reference,5),axis=0,return_inverse=True)
    counts=np.bincount(groups);seams=counts[groups]>1
    if not seams.any():return 0
    state_count=max(len(record['primitive'].get('targets',[])) for record in records)
    for record in records:
        primitive=record['primitive'];attrs=primitive['attributes']
        local=b.accessor_array(doc,data,attrs['POSITION']).copy()
        dense=native_weights(doc,data,attrs);base=posed(local,dense,transforms)[0]
        states=[]
        for state in range(state_count):
            target=primitive.get('targets',[])
            delta=b.accessor_array(doc,data,target[state]['POSITION']) if state<len(target) and 'POSITION' in target[state] else np.zeros_like(local)
            states.append(posed(local+delta,dense,transforms)[0])
        rows.append({'primitive':primitive,'local':local,'dense':dense,'base':base,'states':states})
    labels=np.concatenate([record['labels'] for record in records])
    dense=np.concatenate([row['dense'] for row in rows]);size=len(counts)
    average_weights=np.zeros((size,dense.shape[1]));np.add.at(average_weights,groups,dense)
    average_weights/=counts[:,None]
    capacity=min(8 if 'JOINTS_1' in row['primitive']['attributes'] else 4 for row in rows)
    average_weights=limited_weights(average_weights,capacity)
    shared_dense=dense.copy();shared_dense[seams]=average_weights[groups[seams]]
    def joined(values):
        mean=np.zeros((size,3));np.add.at(mean,groups,values);mean/=counts[:,None]
        if hasattr(envelope,'garment_volume'):
            # A seam can join sleeve and torso cages. Respect both surfaces
            # without separating the two copies again after projection.
            for _ in range(2):
                for label in np.unique(labels[seams]):
                    ids=np.unique(groups[seams&(labels==label)])
                    mean[ids]=envelope.garment_volume(mean[ids],np.full(len(ids),label),clearance,fit=False)[0]
            if top_role=='outer' and hasattr(envelope,'layered_hips'):
                ids=np.flatnonzero(counts>1);mean[ids]=envelope.layered_hips(mean[ids],np.full(len(ids),'torso'))
        result=values.copy();result[seams]=mean[groups[seams]];return result
    base=joined(np.concatenate([row['base'] for row in rows]))
    states=[joined(np.concatenate([row['states'][state] for row in rows])) for state in range(state_count)]
    offset=0
    for row in rows:
        primitive=row['primitive'];attrs=primitive['attributes'];count=len(row['base']);selected=slice(offset,offset+count);offset+=count
        weights=shared_dense[selected];_,matrices=posed(np.zeros((count,3)),weights,transforms)
        inverse=np.linalg.inv(matrices)
        local=np.einsum('nij,nj->ni',inverse[:,:3,:3],base[selected])+inverse[:,:3,3]
        attrs['POSITION']=writer.array(local,'VEC3',bounds=True,target=34962)
        if weights.shape[1]:
            slots=8 if 'JOINTS_1' in attrs else 4
            joints=np.argsort(-weights,axis=1)[:,:min(slots,weights.shape[1])]
            amounts=np.take_along_axis(weights,joints,axis=1);amounts/=np.maximum(amounts.sum(1,keepdims=True),1e-8)
            joints=np.pad(joints,((0,0),(0,slots-joints.shape[1])));amounts=np.pad(amounts,((0,0),(0,slots-amounts.shape[1])))
            for channel in range(slots//4):
                attrs['JOINTS_'+str(channel)]=writer.array(joints[:,channel*4:channel*4+4],'VEC4',5123,target=34962)
                attrs['WEIGHTS_'+str(channel)]=writer.array(amounts[:,channel*4:channel*4+4],'VEC4',target=34962)
        for state,target in enumerate(primitive.get('targets',[])):
            support=np.einsum('nij,nj->ni',inverse[:,:3,:3],states[state][selected])+inverse[:,:3,3]
            target['POSITION']=writer.array(support-local,'VEC3',target=34962)
        indices=b.accessor_array(doc,data,primitive['indices']).astype(int);tri=indices.reshape(-1,3)
        normals=np.zeros_like(local);face=np.cross(local[tri[:,1]]-local[tri[:,0]],local[tri[:,2]]-local[tri[:,0]])
        for column in range(3):np.add.at(normals,tri[:,column],face)
        length=np.linalg.norm(normals,axis=1,keepdims=True);fallback=b.accessor_array(doc,data,attrs['NORMAL']).copy()
        normals=np.divide(normals,length,out=fallback,where=length>1e-10)
        attrs['NORMAL']=writer.array(normals,'VEC3',target=34962)
        if 'TEXCOORD_0' in attrs:attrs['TANGENT']=writer.array(c.tangents(local,normals,b.accessor_array(doc,data,attrs['TEXCOORD_0']),indices),'VEC4',target=34962)
    return int(seams.sum())


def fit_glb(source,envelope,output,clearance=.014,lower_body=False,footwear=False,top_role=None):
    import cp77 as c
    doc,data=c.load_glb(source);writer=b.GLB();writer.doc=copy.deepcopy(doc);writer.bin=bytearray(data)
    transforms=pose_transforms(doc,data,getattr(envelope,'target_world',None))
    lower_body=lower_body or footwear
    layered=top_role=='outer'
    if top_role=='inner' and hasattr(envelope,'joint_nodes'):
        hips=next(n['translation'][1] for n in envelope.joint_nodes if n['name']=='Hips')
        low=total=0
        for mesh in doc['meshes']:
            for primitive in mesh['primitives']:
                attributes=primitive['attributes'];labels=vertex_groups(doc,data,primitive)
                values=posed(b.accessor_array(doc,data,attributes['POSITION']),native_weights(doc,data,attributes),transforms)[0]
                torso=np.isin(labels,['torso','lleg','rleg']);low+=int((torso&(values[:,1]<hips-.06)).sum());total+=int(torso.sum())
        layered=low>=max(3,total*.01)
    source_floor=min(float(posed(b.accessor_array(doc,data,p['attributes']['POSITION']),native_weights(doc,data,p['attributes']),transforms)[0][:,1].min()) for m in doc['meshes'] for p in m['primitives']) if footwear else 0
    stats={'moved_vertices':0,'max_displacement':0.,'weights':'native rig; contact follows body; distant cloth preserved','subdivided_primitives':0,'morph_targets':0}
    stats['hip_layer']=top_role if layered else None
    seam_records=[]
    for mesh in writer.doc['meshes']:
        for p in mesh['primitives']:
            a=p['attributes'];tri=b.accessor_array(doc,data,p['indices']).astype(int).reshape(-1,3)
            used,idx=np.unique(tri,return_inverse=True);tri=idx.reshape(-1,3)
            values={key:b.accessor_array(doc,data,acc)[used] for key,acc in a.items()}
            morphs=[{key:b.accessor_array(doc,data,acc)[used] for key,acc in target.items()} for target in p.get('targets',[])]
            dense=native_weights(doc,data,a,used)
            labels=vertex_groups(doc,data,p)[used]
            positions=values['POSITION']
            edges,inverse=np.unique(np.sort(np.concatenate([tri[:,[0,1]],tri[:,[1,2]],tri[:,[2,0]]]),axis=1),axis=0,return_inverse=True)
            # A coarse garment face can cut through a curved thigh even when
            # its corner vertices clear the skin. Split once, within native limits.
            if len(tri)*4<=65000 and len(positions)+len(edges)<=60000 and np.linalg.norm(positions[edges[:,0]]-positions[edges[:,1]],axis=1).max(initial=0)>.075:
                count=len(positions);ab,bc,ca=(inverse.reshape(3,-1)+count)
                original_tri=tri
                tri=np.concatenate([np.column_stack([original_tri[:,0],ab,ca]),np.column_stack([ab,original_tri[:,1],bc]),
                                    np.column_stack([ca,bc,original_tri[:,2]]),np.column_stack([ab,bc,ca])])
                for key,value in values.items():values[key]=np.concatenate([value,(value[edges[:,0]]+value[edges[:,1]])*.5])
                if dense.shape[1]:dense=np.concatenate([dense,(dense[edges[:,0]]+dense[edges[:,1]])*.5])
                for target in morphs:
                    for key,value in target.items():target[key]=np.concatenate([value,(value[edges[:,0]]+value[edges[:,1]])*.5])
                labels=np.concatenate([labels,labels[edges[:,0]]]);positions=values['POSITION']
                stats['subdivided_primitives']+=1
            positions,bind_matrices=posed(positions,dense,transforms)
            seam_reference=positions.copy()
            if lower_body:
                if not hasattr(envelope,'lower_body_cage'):envelope.lower_body_cage=LowerBodyCage(envelope)
                cage=envelope.lower_body_cage
                probe=positions.copy()
                if footwear:
                    sole=1-np.clip((probe[:,1]-source_floor)/max(cage.foot_height+.15-source_floor,.05),0,1)
                    probe[:,1]+=(cage.floor-source_floor)*sole
                target=cage.project(probe,clearance,footwear,fit=not footwear)
                relax=seam_relaxer(positions,tri,1.)
                fitted=positions+relax(target-positions)
                fitted=cage.project(fitted,clearance,footwear)
                distance=np.linalg.norm(fitted-positions,axis=1)
                info={'moved_vertices':int((distance>1e-6).sum()),'max_displacement':float(distance.max(initial=0))}
                positions=fitted
            else:
                target,info=(envelope.garment_volume(positions,labels,clearance) if hasattr(envelope,'garment_volume') else envelope.project(positions,labels,clearance))
                relax=seam_relaxer(positions,tri,1.)
                positions=positions+relax(target-positions)
                if hasattr(envelope,'garment_volume'):positions=envelope.garment_volume(positions,labels,clearance,fit=False)[0]
            stats['moved_vertices']+=info['moved_vertices'];stats['max_displacement']=max(stats['max_displacement'],info['max_displacement'])
            hip_extra=np.zeros_like(positions)
            if layered and hasattr(envelope,'layered_hips'):
                before_layer=positions.copy();positions=envelope.layered_hips(positions,labels);hip_extra=positions-before_layer
            # Contact fabric follows the avatar's skinning through a bend.
            # Keep every native bone and preserve distant hems/dangle panels.
            dense=follow_body_weights(positions,labels,dense,doc,envelope)
            # The binding must use the exact 4/8 influences written below.
            # Truncating afterward changes the pose after native import.
            dense=limited_weights(dense,8 if 'JOINTS_1' in values else 4)
            if dense.shape[1]:
                _,bind_matrices=posed(values['POSITION'],dense,transforms)
            inverse=np.linalg.inv(bind_matrices)
            values['POSITION']=np.einsum('nij,nj->ni',inverse[:,:3,:3],positions)+inverse[:,:3,3]
            positions=values['POSITION']
            # Garment Support can shrink a correctly fitted base back through
            # the avatar. Constrain every support state against the same body.
            if hasattr(envelope,'garment_volume'):
                for target in morphs:
                    if 'POSITION' not in target:continue
                    support=posed(positions+target['POSITION'],dense,transforms)[0]
                    if layered and top_role=='inner':support-=hip_extra
                    if lower_body:support=envelope.lower_body_cage.project(support,clearance,footwear)
                    else:support=envelope.garment_volume(support,labels,clearance,fit=False)[0]
                    # Outer coats remain outside trousers even when crushed;
                    # an inner shirt may still use its native tucked state.
                    if top_role=='outer' and hasattr(envelope,'layered_hips'):support=envelope.layered_hips(support,labels)
                    support=np.einsum('nij,nj->ni',inverse[:,:3,:3],support)+inverse[:,:3,3]
                    target['POSITION']=support-positions
            if dense.shape[1]:
                count=8 if 'JOINTS_1' in values else 4
                joints=np.argsort(-dense,axis=1)[:,:min(count,dense.shape[1])]
                weights=np.take_along_axis(dense,joints,axis=1)
                weights/=np.maximum(weights.sum(1,keepdims=True),1e-8)
                joints=np.pad(joints,((0,0),(0,count-joints.shape[1])))
                weights=np.pad(weights,((0,0),(0,count-weights.shape[1])))
                for channel in range(count//4):
                    values[f'JOINTS_{channel}']=joints[:,channel*4:channel*4+4]
                    values[f'WEIGHTS_{channel}']=weights[:,channel*4:channel*4+4]
            a.clear()
            for key,value in values.items():
                if key in ['NORMAL','TANGENT']:continue
                kind='SCALAR' if value.ndim==1 else 'VEC'+str(value.shape[1])
                a[key]=writer.array(value,kind,5123 if key.startswith('JOINTS_') else 5126,bounds=key=='POSITION',target=34962)
            # Morph deltas use the same compacted/subdivided vertex order as
            # the base. The old accessors referred to a different topology.
            if morphs:
                p['targets']=[{key:writer.array(value,'VEC3',target=34962) for key,value in target.items()} for target in morphs]
                stats['morph_targets']+=len(morphs)
            idx=tri.reshape(-1);p['indices']=writer.array(idx,'SCALAR',5125,target=34963)
            normal=np.zeros_like(positions);face=np.cross(positions[tri[:,1]]-positions[tri[:,0]],positions[tri[:,2]]-positions[tri[:,0]])
            for col in range(3):np.add.at(normal,tri[:,col],face)
            length=np.linalg.norm(normal,axis=1,keepdims=True)
            original=values.get('NORMAL',np.tile([0.,1.,0.],(len(normal),1)))
            original=original.copy()
            bad=np.linalg.norm(original,axis=1)<1e-8;original[bad]=[0.,1.,0.]
            original/=np.linalg.norm(original,axis=1,keepdims=True)
            normal=np.divide(normal,length,out=original.copy(),where=length>1e-10)
            a['NORMAL']=writer.array(normal,'VEC3',target=34962)
            if 'TEXCOORD_0' in a:
                uv=values['TEXCOORD_0']
                a['TANGENT']=writer.array(c.tangents(positions,normal,uv,idx),'VEC4',target=34962)
            seam_records.append({'primitive':p,'reference':seam_reference,'labels':labels.copy()})
    stats['stitched_vertices']=stitch_fitted_seams(writer,seam_records,transforms,envelope,clearance,top_role)
    # SharpGLTF requires one common scene root for mesh and skin joints.
    for scene in writer.doc['scenes']:
        if len(scene['nodes'])>1:
            root=len(writer.doc['nodes']);writer.doc['nodes'].append({'name':'AvatarBridge','children':scene['nodes'][:]})
            scene['nodes']=[root]
    writer.save(output)
    return stats


def import_refits(glbs,directory):
    import cp77 as c
    settings=c.cli().parent/'appsettings.json'
    original=settings.read_text(encoding='utf-8-sig')
    config=json.loads(original);config.setdefault('GltfImportArgs',{})['ImportGarmentSupport']=True
    settings.write_text(json.dumps(config,indent=2),encoding='utf-8')
    try:
        batches=[glbs[start:start+16] for start in range(0,len(glbs),16)]
        # Each process writes different meshes. Keep settings fixed until both
        # workers finish, including when an import raises an exception.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=[pool.submit(c.command,'import',*batch,'-o',directory,'--keep') for batch in batches]
            for task in pending:task.result()
    finally:settings.write_text(original,encoding='utf-8')


def build(game,code,parts,model_dir,prefix,groups=None):
    import cp77 as c
    refs=b.DATA/'garment-reference'/code;refs.mkdir(parents=True,exist_ok=True)
    raw=refs/'raw';raw.mkdir(exist_ok=True)
    c.command('unbundle',Path(game)/'archive/pc/content/basegame_4_appearance.archive','-o',refs,
              '-r',rf'base\\characters\\garment\\.*{code}.*\.mesh$')
    sources=sorted(refs.rglob('*.mesh'))
    sources=[p for p in sources if 'shadow' not in p.name.lower()]
    if groups is not None:
        sources=[p for p in sources if any('/'+group+'/' in p.relative_to(refs).as_posix() for group in groups)]
    if len({p.name for p in sources})!=len(sources):raise ValueError('Nomi duplicati nelle mesh dei vestiti')
    missing=[p for p in sources if not (raw/(p.stem+'.glb')).is_file()]
    for start in range(0,len(missing),48):c.command('export',*missing[start:start+48],'-o',raw)
    directory=Path(model_dir)/'garments';directory.mkdir(exist_ok=True)
    gender,body=('female','woman') if code=='pwa' else ('male','man')
    rig_dir=b.DATA/'build-reference'/gender
    rigs=list(rig_dir.glob(f'player_{body}_base_deformations.rig.json')) or list(rig_dir.glob(f'player_{body}_fpp_deformations.rig.json'))
    if not rigs:raise ValueError('Rig di riferimento del giocatore non trovato per il refit.')
    envelope=Envelope(parts,json.loads(rigs[0].read_text(encoding='utf-8-sig')));catalog={};stats=[]
    coverage=ClothingCoverage(parts,envelope.target_world)
    envelope.lower_body_cage=LowerBodyCage(envelope)
    envelope.volume_cages={label:RadialCage(envelope,label) for label in ['torso','larm','rarm']}
    def prepare_one(source):
        glb=raw/(source.stem+'.glb')
        if not glb.is_file():return None
        path=str(source.relative_to(refs));key=fnv_path(path);label='g_'+format(int(key),'016x')
        shutil.copy2(source,directory/(label+'.mesh'))
        clearance=.024 if source.name.startswith('t2_') else .025 if source.name.startswith('l1_') else .015 if source.name.startswith('s1_') else .018
        if '_dec_' in source.name:clearance+=.004
        role='outer' if source.name.startswith('t2_') else 'inner' if source.name.startswith('t1_') else None
        info=fit_glb(glb,envelope,directory/(label+'.glb'),clearance,source.name.startswith('l1_'),source.name.startswith('s1_'),top_role=role)
        covered=coverage.garment(directory/(label+'.glb'),path)
        return key,{'mesh':prefix+'\\garments\\'+label+'.mesh','source':path,'hide_regions':covered['regions'],
                    'coverage_chunk_count':covered.get('chunk_count',0)},{'source':path,**info,'coverage':covered}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(prepare_one,sources):
            if result is None:continue
            key,entry,info=result;catalog[key]=entry;stats.append(info)
    glbs=sorted(directory.glob('*.glb'))
    import_refits(glbs,directory)
    for p in directory.glob('*.glb'):p.unlink()
    (Path(model_dir).parent.parent.parent/'garment-refit.json').write_text(json.dumps(stats,indent=2))
    return catalog
