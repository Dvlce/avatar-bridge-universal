"""Generate a small native REDengine dangle rig for an avatar tail."""
import copy
import re
import numpy as np


def tail_chain(names):
    """Conservative labels: never interpret an accessory called tailcoat as anatomy."""
    candidates=[n for n in names if re.fullmatch(r"(?:tail(?:_?rt|_?root)|tail_?\d+)",n,re.I)]
    return sorted(candidates,key=lambda n:(-1 if re.search(r'rt|root',n,re.I) else int(re.search(r'\d+',n)[0])))[:8]


def frame_x(direction):
    x=direction/max(np.linalg.norm(direction),1e-8)
    reference=np.array([0.,1.,0.])
    if abs(x@reference)>.95:reference=np.array([0.,0.,1.])
    z=np.cross(x,reference);z/=max(np.linalg.norm(z),1e-8)
    return np.column_stack([x,np.cross(z,x),z])


def add_targets(source_names,source_world,transforms,targets,aliases):
    chain=tail_chain(source_names)
    if len(chain)<2:return []
    hips=source_names.index('Hips');deform=transforms[hips]
    positions=[(deform@source_world[source_names.index(n)])[:3,3] for n in chain]
    for i,n in enumerate(chain):
        direction=positions[min(i+1,len(chain)-1)]-positions[max(0,i-1)]
        world=np.eye(4);world[:3,:3]=frame_x(direction);world[:3,3]=positions[i]
        alias=f'dyng_avatar_tail_{i:02}'
        targets[alias]=world
        index=source_names.index(n);aliases[index]=alias;transforms[index]=deform
    return [f'dyng_avatar_tail_{i:02}' for i in range(len(chain))]


def resources(template,targets,tail_names,directory,mesh_bones=None):
    """Use only native type schemas; no copied third-party physics resources."""
    import cp77 as c
    if not tail_names:return None
    primary=[n for n in ['Root','Hips','Spine1','LeftUpLeg','LeftLeg','RightUpLeg','RightLeg'] if n in targets]
    parent_by_name={'Hips':'Root','Spine1':'Hips','LeftUpLeg':'Hips','RightUpLeg':'Hips','LeftLeg':'LeftUpLeg','RightLeg':'RightUpLeg'}
    native=template.get('Data',{}).get('RootChunk',{})
    native_names=[n.get('$value',n.get('Value','')) if isinstance(n,dict) else n for n in native.get('boneNames',[])]
    for index,parent in enumerate(native.get('boneParentIndexes',[])):
        if index<len(native_names):parent_by_name[native_names[index]]=native_names[parent] if 0<=parent<len(native_names) else None
    required=set(primary)|set(mesh_bones or [])-set(tail_names)
    for name in list(required):
        visited=set()
        while name and name not in visited:
            visited.add(name)
            if name in targets:required.add(name)
            name=parent_by_name.get(name)
    primary=[n for n in targets if n in required and n not in tail_names]
    names=primary+tail_names
    parents=[];transforms=[]
    conversion=np.array([[1.,0,0,0],[0,0,1.,0],[0,-1.,0,0],[0,0,0,1.]])
    for i,n in enumerate(names):
        if n=='Root':parent=None
        elif n in tail_names:parent='Hips' if n==tail_names[0] else tail_names[tail_names.index(n)-1]
        else:parent=parent_by_name.get(n)
        parent=parent if parent in names else None
        parents.append(names.index(parent) if parent else -1)
        local=np.linalg.inv(targets[parent])@targets[n] if parent else targets[n]
        t=c.trs(np.linalg.inv(conversion)@local@conversion);q=t['rotation'];p=t['translation']
        transforms.append({'$type':'QsTransform','Rotation':{'$type':'Quaternion','i':q[0],'j':q[1],'k':q[2],'r':q[3]},
            'Translation':{'$type':'Vector4','X':p[0],'Y':p[1],'Z':p[2],'W':0},
            'Scale':{'$type':'Vector4','X':1,'Y':1,'Z':1,'W':1}})
    doc=copy.deepcopy(template)
    doc['Data']['RootChunk']={'$type':'animRig','boneNames':[c.name(n) for n in names],
        'boneParentIndexes':parents,'boneTransforms':transforms,'turnOffLOD':1}
    c.deserialize(doc,'tail.rig',directory)
    handle_id=0
    animation_nodes=[]
    def handle(data):
        nonlocal handle_id
        handle_id+=1
        result={'HandleId':str(50000+handle_id),'Data':data}
        if data.get('$type','').startswith('animAnimNode_'):animation_nodes.append(result)
        return result
    def bone(n):return {'$type':'animTransformIndex','name':c.name(n)}
    def link(node):return {'$type':'animPoseLink','node':node}
    constraints=[]
    for a,z in zip(tail_names,tail_names[1:]):
        constraints.append(handle({'$type':'animDyngConstraintLink','bone1':bone(a),'bone2':bone(z),
            'lengthLowerBoundRatioPercentage':100,'lengthUpperBoundRatioPercentage':100,
            'linkType':'KeepFixedDistance','lookAtAxis':{'$type':'Vector3','X':1,'Y':0,'Z':0}}))
    particles=[{'$type':'animDyngParticle','bone':bone(n),'damping':3.,'mass':.2,
        'isFree':int(i>0),'pullForceFactor':2.,'collisionCapsuleRadius':.035,
        'collisionCapsuleHeightExtent':0,'collisionCapsuleAxisLS':{'$type':'Vector3','X':1,'Y':0,'Z':0},
        'projectionType':'ShortestPath'} for i,n in enumerate(tail_names)]
    simulation=handle({'$type':'animDangleConstraint_SimulationDyng','alpha':1,
        'rotateParentToLookAtDangle':1,'solverIterations':8,'substepTime':1/120,
        'HACK_checkDangleTeleport':1,'jsonCollisionShapesLoadedSuccessfully':1,
        'dyngConstraint':handle({'$type':'animDyngConstraintMulti','innerConstraints':constraints}),
        'particlesContainer':{'$type':'animDyngParticlesContainer','gravityWS':2.5,
            'externalForceWS':{'$type':'Vector3','X':0,'Y':0,'Z':0},'particles':particles}})
    node=handle({'$type':'animAnimNode_ReferencePoseTerminator'})
    node=handle({'$type':'animAnimNode_SharedMetaPose','inputLink':link(node)})
    node=handle({'$type':'animAnimNode_PoseLsToMs','inputLink':link(node)})
    node=handle({'$type':'animAnimNode_Dangle','inputLink':link(node),'dangleConstraint':simulation})
    node=handle({'$type':'animAnimNode_PoseMsToLs','inputLink':link(node)})
    node=handle({'$type':'animAnimNode_Output','node':link(node)})
    root=handle({'$type':'animAnimNode_Root','nodes':[node]})
    graph={'$type':'animAnimGraph','nodesToInit':[root]+[{'HandleRefId':n['HandleId']} for n in animation_nodes if n is not root],
        'rootNode':{'HandleRefId':root['HandleId']},
        'variables':handle({'$type':'animAnimVariableContainer'}),
        'timeDeltaMultiplier':1.,'useAnimCommands':0,'useAnimStaticCommands':0}
    doc=copy.deepcopy(template);doc['Data']['RootChunk']=graph
    c.deserialize(doc,'tail.animgraph',directory)
    return {'bones':len(tail_names),'solver':'REDengine dangle particle chain','gravity':2.5,'native_verified':False}


def animated_component(name,prefix,component_id,enabled=True):
    import cp77 as c
    return {'$type':'entAnimatedComponent','id':str(component_id),'name':c.name(name),
        'rig':c.ref(prefix+'\\tail.rig'),'graph':c.ref(prefix+'\\tail.animgraph'),
        'calculateAccelerationWs':1,
        'controlBinding':{'HandleId':str(component_id+10),'Data':{'$type':'entAnimationControlBinding','bindName':c.name('root'),'enabled':1}},
        'parentTransform':{'HandleId':str(component_id+11),'Data':{'$type':'entHardTransformBinding','bindName':c.name('root'),'enabled':1,'slotName':c.name('None')}}}
