import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"engine"))
import bridge
import cp77
import library
import garments
import physics
import geometry
import facial
from scipy.spatial import cKDTree


class GameBuildTests(unittest.TestCase):
    def test_material_seams_stay_closed_in_bent_and_compressed_states(self):
        writer=bridge.GLB();writer.doc['nodes']=[{'name':'bone0'},{'name':'bone1'}]
        writer.doc['skins']=[{'joints':[0,1]}];writer.doc['meshes']=[];records=[]
        references=[np.array([[0,1,0],[-.2,1,0],[-.2,1.2,0]]),np.array([[0,1,0],[.2,1,0],[.2,1.2,0]])]
        originals=[]
        for index,reference in enumerate(references):
            points=reference+[0,0,.02];points=points.copy();points[0,0]+=.03*index;originals.append(points.copy())
            skin_weights=np.tile([1.,0,0,0],(3,1));skin_weights[1]=0
            attrs={'POSITION':writer.array(points,'VEC3'),
                   'NORMAL':writer.array(np.tile([0.,0.,1.],(3,1)),'VEC3'),
                   'TEXCOORD_0':writer.array(np.full((3,2),index),'VEC2'),
                   'JOINTS_0':writer.array(np.full((3,4),index),'VEC4',5123),
                   'WEIGHTS_0':writer.array(skin_weights,'VEC4')}
            primitive={'attributes':attrs,'indices':writer.array(np.array([0,1,2]),'SCALAR',5125),
                       'targets':[{'POSITION':writer.array(np.tile([0.,0.,-.01-.03*index],(3,1)),'VEC3')}]}
            writer.doc['meshes'].append({'primitives':[primitive]})
            records.append({'primitive':primitive,'reference':reference,'labels':np.full(3,'torso' if index==0 else 'rarm')})
        transforms=np.tile(np.eye(4),(2,1,1))
        self.assertEqual(garments.stitch_fitted_seams(writer,records,transforms,object(),.018),2)
        doc,data=writer.doc,bytes(writer.bin)
        bent=transforms.copy();bent[1,0,3]=.2
        for pose in [transforms,bent]:
            for state in [0.,.5,1.]:
                points=[]
                for mesh in doc['meshes']:
                    primitive=mesh['primitives'][0];attrs=primitive['attributes']
                    vertices=bridge.accessor_array(doc,data,attrs['POSITION'])+state*bridge.accessor_array(doc,data,primitive['targets'][0]['POSITION'])
                    points.append(garments.posed(vertices,garments.native_weights(doc,data,attrs),pose)[0][0])
                np.testing.assert_allclose(points[0],points[1],atol=1e-6)
        for index,mesh in enumerate(doc['meshes']):
            primitive=mesh['primitives'][0];attrs=primitive['attributes']
            np.testing.assert_array_equal(bridge.accessor_array(doc,data,primitive['indices']).ravel(),[0,1,2])
            np.testing.assert_array_equal(bridge.accessor_array(doc,data,attrs['TEXCOORD_0']),np.full((3,2),index))
            np.testing.assert_allclose(bridge.accessor_array(doc,data,attrs['POSITION'])[1:],originals[index][1:],atol=1e-6)

    def test_torso_cage_does_not_collapse_compressed_cloth_into_missing_back_slice(self):
        envelope=object.__new__(garments.Envelope)
        envelope.vertices=np.array([[x,y,z] for y in [.8,1.2,1.4,1.5]
                                    for x,z in [(-.2,-.15),(.2,-.15),(.2,.15),(-.2,.15)]])
        faces=[]
        for level in range(3):
            for side in range(4):
                if level==1 and side!=0:continue
                a=level*4+side;d=level*4+(side+1)%4
                faces.extend([[a,d,a+4],[d,d+4,a+4]])
        envelope.triangles=np.array(faces)
        envelope.groups={'all':(np.arange(len(faces)),None),'torso':(np.arange(len(faces)),None)}
        envelope.joint_nodes=[{'name':name,'translation':[0,y,0]} for name,y in
                              [('Hips',.85),('Spine1',1.1),('Spine3',1.3),('Neck',1.5)]]
        cage=garments.RadialCage(envelope,'torso')
        points=np.array([[0,1.3,.02],[0,1.3,-.2],[0,1.7,.02]])
        result=cage.project(points,.018,fit=False)
        self.assertGreaterEqual(result[0,2],.167)
        np.testing.assert_array_equal(result[1:],points[1:])

    def test_dorsal_fit_bridges_missing_posterior_slice_without_moving_skin(self):
        vertices=np.array([[x,y,z] for y in [.8,1.2,1.4,1.5]
                           for x,z in [(-.2,-.15),(.2,-.15),(.2,.15),(-.2,.15)]])
        faces=[]
        for level in range(3):
            for side in range(4):
                if level==1 and side!=0:continue
                a=level*4+side;d=level*4+(side+1)%4
                faces.extend([[a,d,a+4],[d,d+4,a+4]])
        v=np.concatenate([vertices,[[-.05,1.26,.15],[.05,1.26,.15],[0,1.3,.27]]])
        triangles=np.array(faces+[[16,17,18]])
        uv=np.zeros((len(v),2));uv[16:]=.5
        names=['Hips','Spine1','Spine3','Neck','Head']
        j=np.zeros((len(v),4),dtype=int);j[16:,0]=2
        w=np.tile([1.,0,0,0],(len(v),1));normals=np.tile([0.,0.,1.],(len(v),1))
        anchors={'Hips':[0,.85,0],'Spine1':[0,1.1,0],'Spine3':[0,1.3,0],
                 'Neck':[0,1.5,0],'LeftArm':[-.2,1.45,0],'RightArm':[.2,1.45,0]}
        p,_,_,_,report=geometry.fit_dorsal_shells(v,normals,uv,triangles,j,w,anchors,names)
        np.testing.assert_array_equal(p[:16],v[:16])
        self.assertAlmostEqual(p[18,2],.153,places=5)
        self.assertGreater(report['bridged_posterior_vertices'],0)

    def test_outer_hip_layer_clears_trousers_and_preserves_collar_hems_and_arms(self):
        envelope=object.__new__(garments.Envelope)
        envelope.joint_nodes=[{'name':'Hips','translation':[0,1.,0]}]
        class Cylinder:
            def project(self,p,clearance,fit=False):
                result=p.copy();length=np.linalg.norm(p[:,[0,2]],axis=1)
                result[:,[0,2]]*=np.maximum(length,.1+clearance)[:,None]/length[:,None]
                return result
        envelope.lower_body_cage=Cylinder()
        points=np.array([[.125,.9,0.],[.125,1.,0.],[.125,1.18,0.],[.125,.85,0.],[.3,.9,0.],[.125,.4,0.],[.125,1.4,0.],[.125,.9,0.]])
        labels=np.array(['torso']*3+['rleg']+['torso']*3+['larm'])
        result=envelope.layered_hips(points,labels)
        self.assertTrue((result[:4,0]>.143+.016).all())
        np.testing.assert_array_equal(result[4:],points[4:])

    def test_nape_fit_reaches_above_collar_and_preserves_actual_neck_skin(self):
        box=np.array([[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]])
        faces=np.array([[0,1,2],[0,2,3],[4,6,5],[4,7,6],[0,4,5],[0,5,1],
                        [3,2,6],[3,6,7],[0,3,7],[0,7,4],[1,5,6],[1,6,2]])
        torso=box*np.array([.2,.35,.15])+[0,1.15,0]
        neck=box*np.array([.06,.125,.06])+[0,1.595,.03]
        v=np.concatenate([torso,neck,[[-.04,1.49,.15],[.04,1.49,.15],[0,1.52,.26]]])
        tri=np.concatenate([faces,faces+8,[[16,17,18]]]);uv=np.zeros((len(v),2));uv[8:16]=.25;uv[16:]=.5
        names=['Hips','Spine1','Spine3','Neck','Head'];j=np.zeros((len(v),4),dtype=int);j[8:16,0]=3;j[16:18,0]=2;j[18,0]=4
        w=np.tile([1.,0,0,0],(len(v),1));n=np.tile([0.,0.,1.],(len(v),1))
        anchors={'Hips':[0,.85,0],'Spine1':[0,1.1,0],'Spine3':[0,1.3,0],'Neck':[0,1.5,.02],
                 'Head':[0,1.64,0],'LeftArm':[-.2,1.45,0],'RightArm':[.2,1.45,0]}
        p,_,joints,weights,report=geometry.fit_dorsal_shells(v,n,uv,tri,j,w,anchors,names)
        self.assertEqual(report['protected_neck_triangles'],12)
        np.testing.assert_array_equal(p[:16],v[:16])
        self.assertLess(p[18,2],.10)
        self.assertEqual(joints[18,weights[18].argmax()],names.index('Neck'))

    def test_dorsal_fit_preserves_skin_indices_neck_and_unused_vertices(self):
        v=np.array([[-.2,.8,-.15],[.2,.8,-.15],[.2,1.5,-.15],[-.2,1.5,-.15],
                    [-.2,.8,.15],[.2,.8,.15],[.2,1.5,.15],[-.2,1.5,.15],
                    [-.1,1.1,.15],[.1,1.1,.15],[0,1.3,.27],
                    [0,1.56,.27],[.01,1.57,.27],[0,1.58,.28],[0,1.3,.3]])
        t=np.array([[0,1,2],[0,2,3],[4,6,5],[4,7,6],[0,4,5],[0,5,1],
                    [3,2,6],[3,6,7],[0,3,7],[0,7,4],[1,5,6],[1,6,2],[8,9,10],[11,12,13]])
        uv=np.zeros((len(v),2));uv[8:]=.5
        n=np.tile([0.,0.,1.],(len(v),1));j=np.zeros((len(v),4),dtype=int);w=np.tile([1.,0,0,0],(len(v),1))
        names=['Hips','Spine1','Spine3','Neck','Head'];j[8:,0]=4;j[8:10,0]=2
        anchors={'Hips':[0,.85,0],'Spine1':[0,1.1,0],'Spine3':[0,1.3,0],'Neck':[0,1.5,0],
                 'LeftArm':[-.2,1.45,0],'RightArm':[.2,1.45,0]}
        original=v.copy();indices=t.copy()
        p,_,joints,weights,report=geometry.fit_dorsal_shells(v,n,uv,t,j,w,anchors,names)
        self.assertGreater(report['moved_vertices'],0)
        np.testing.assert_array_equal(p[:8],original[:8])
        np.testing.assert_array_equal(p[11:],original[11:])
        np.testing.assert_array_equal(v,original);np.testing.assert_array_equal(t,indices)
        self.assertAlmostEqual(p[10,2],.153,places=5)
        self.assertEqual(joints[10,weights[10].argmax()],names.index('Spine3'))
        np.testing.assert_allclose(weights.sum(1),1.)
        # A seam vertex can have little tail weight while its triangle still
        # belongs to the tail. The entire tail triangle must stay protected.
        protected=geometry.fit_dorsal_shells(v,n,uv,t,j,w,anchors,names,excluded_vertices=np.array([10]))[0]
        np.testing.assert_array_equal(protected,original)

    def test_closed_trouser_radius_keeps_small_real_skin_extremes(self):
        count=96;angles=np.arange(count)*2*np.pi/count
        v=np.array([[.15*np.cos(a),y,.15*np.sin(a)] for y in [0.,1.] for a in angles])
        faces=[]
        for i in range(count):
            j=(i+1)%count;faces.extend([[i,j,i+count],[j,j+count,i+count]])
        coverage=object.__new__(garments.ClothingCoverage)
        skin=np.array([[.1*np.cos(a),y,.1*np.sin(a)] for y in [.3,.7] for a in angles])
        # A small fur/skin protrusion is real geometry even when it represents
        # much less than5% of the vertices near a slice.
        coverage.section_vertices={'lleg_1':np.concatenate([skin,np.array([[.12,y,0.] for y in np.linspace(.3,.7,11)])])}
        coverage.target_world={}
        for name,y in [('LeftUpLeg',1.),('LeftLeg',.5),('LeftFoot',0.)]:
            m=np.eye(4);m[1,3]=y;coverage.target_world[name]=m
        self.assertTrue(coverage.closed_leg(v[np.array(faces)],'lleg_1'))

    def test_closed_trouser_section_rejects_slits_cuffs_and_other_leg(self):
        count=96;angles=np.arange(count)*2*np.pi/count
        vertices=np.array([[.12*np.cos(a),y,.12*np.sin(a)] for y in [0.,.405,.406,1.] for a in angles])
        faces=[]
        for row in range(3):
            for i in range(count):
                j=(i+1)%count;a=row*count+i;z=row*count+j
                faces.extend([[a,z,a+count],[z,z+count,a+count]])
        triangles=vertices[np.array(faces)]
        coverage=object.__new__(garments.ClothingCoverage)
        coverage.section_vertices={'lleg_1':np.array([[.1*np.cos(a),y,.1*np.sin(a)] for y in [.3,.7] for a in angles])}
        coverage.target_world={}
        for name,y in [('LeftUpLeg',1.),('LeftLeg',.5),('LeftFoot',0.)]:
            m=np.eye(4);m[1,3]=y;coverage.target_world[name]=m
        self.assertTrue(coverage.closed_leg(triangles,'lleg_1'))
        # This one-millimetre slit lies between the sampled ring heights.
        # Open boundary edges must still reject the section.
        slit=np.delete(triangles,[count*2,count*2+1],axis=0)
        self.assertFalse(coverage.closed_leg(slit,'lleg_1'))
        other=triangles+np.array([.25,0.,0.])
        self.assertFalse(coverage.closed_leg(np.concatenate([slit,other]),'lleg_1'))
        cuff=triangles.copy();cuff[:,:,1]*=.5
        self.assertFalse(coverage.closed_leg(cuff,'lleg_1'))
        self.assertFalse(coverage.closed_leg(triangles,'torso_middle'))

    def test_contact_binding_uses_the_final_eight_native_influences(self):
        writer=bridge.GLB();points=np.array([[0.,0.,0.],[.02,0.,0.],[0.,.02,0.]])
        writer.doc['nodes']=[{'name':'bone'+str(i)} for i in range(9)]
        writer.doc['skins']=[{'joints':list(range(9)),'inverseBindMatrices':writer.array(np.tile(np.eye(4).reshape(1,16),(9,1)),'MAT4')}]
        attrs={'POSITION':writer.array(points,'VEC3')}
        for channel in [0,1]:
            attrs['JOINTS_'+str(channel)]=writer.array(np.tile(np.arange(1+channel*4,5+channel*4),(3,1)),'VEC4',5123)
            attrs['WEIGHTS_'+str(channel)]=writer.array(np.full((3,4),.125),'VEC4')
        writer.doc['meshes']=[{'primitives':[{'attributes':attrs,'indices':writer.array(np.array([0,1,2]),'SCALAR',5123)}]}]
        writer.doc['nodes'].append({'mesh':0,'skin':0});writer.doc['scenes'][0]['nodes']=list(range(10))
        class Skin:
            skin_weights=np.ones((1,1));joint_names=['bone0']
            target_world={'bone'+str(i):np.eye(4) for i in range(9)}
            target_world['bone0']=np.eye(4);target_world['bone0'][0,3]=1.
            def project(self,p,labels,margin):return p+np.array([0.,0.,.02]),{'moved_vertices':len(p),'max_displacement':.02}
            def nearest(self,p,label):
                closest=p.copy();closest[:,2]=0
                return closest,np.zeros((len(p),3),dtype=int)
            def weights(self,p,labels):return np.zeros((len(p),4),dtype=int),np.tile([1.,0,0,0],(len(p),1))
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.glb';target=Path(folder)/'target.glb';writer.save(source)
            garments.fit_glb(source,Skin(),target)
            d,data=cp77.load_glb(target);attrs=d['meshes'][0]['primitives'][0]['attributes']
            weights=garments.native_weights(d,data,attrs)
            self.assertTrue(((weights>0).sum(1)<=8).all())
            actual,_=garments.posed(bridge.accessor_array(d,data,attrs['POSITION']),weights,garments.pose_transforms(d,data,Skin.target_world))
            np.testing.assert_allclose(actual,points+[0.,0.,.02],atol=1e-6)

    def test_contact_fabric_follows_skin_and_distant_cloth_keeps_its_bones(self):
        class Skin:
            skin_weights=np.ones((1,1));joint_names=['LeftLeg']
            def nearest(self,points,label):
                closest=points.copy();closest[:,2]=0
                return closest,np.zeros((len(points),3),dtype=int)
            def weights(self,points,labels):
                return np.zeros((len(points),4),dtype=int),np.tile([1.,0,0,0],(len(points),1))
        doc={'nodes':[{'name':'LeftLeg'},{'name':'dyng_coat'}],'skins':[{'joints':[0,1]}]}
        points=np.array([[.1,.5,.02],[.1,.5,.2]])
        original=np.tile([0.,1.],(2,1))
        weights=garments.follow_body_weights(points,np.array(['lleg','lleg']),original,doc,Skin())
        np.testing.assert_allclose(weights[0],[.85,.15])
        np.testing.assert_array_equal(weights[1],original[1])
        np.testing.assert_allclose(weights.sum(1),1.)
        # A bend which moves the cloth bone away from the skin must have
        # substantially less separation on the contact patch.
        transforms=np.tile(np.eye(4),(2,1,1));transforms[1,2,3]=.1
        moved,_=garments.posed(points,weights,transforms)
        self.assertAlmostEqual(moved[0,2]-points[0,2],.015)
        doc['nodes'][0]['name']='unknown_anchor'
        np.testing.assert_array_equal(garments.follow_body_weights(points,np.array(['lleg','lleg']),original,doc,Skin()),original)

    def test_smoothed_cage_never_loses_a_local_skin_protrusion(self):
        radii=np.full((7,96),.1);radii[3,0]=.12
        result=garments.conservative_radii(radii)
        self.assertTrue((result>=radii).all())
        self.assertGreater(result[3,-1],.1)
        self.assertLessEqual(result.max(),.12)

    def test_open_garment_cannot_hide_mostly_covered_skin_sections(self):
        # The actual short-sleeve Maelstrom jacket covers 94% of the belly
        # section and 95-97% of the upper arms, leaving visible openings.
        fractions={'torso_middle':.943,'larm_0':.952,'rarm_0':.968,'torso_upper':.882}
        self.assertEqual(garments.covered_regions(fractions,r'base\torso\jacket.mesh'),[])
        self.assertEqual(garments.covered_regions({'larm_1':1.,'torso_lower':.996},r'base\torso\shirt.mesh'),
                         ['larm_1','torso_lower'])

    def test_coverage_chunk_counts_map_source_and_fitted_mesh(self):
        catalog={'7':{'source':r'base\torso\shirt.mesh','mesh':r'avatarbridge\shirt.mesh',
                      'hide_regions':['larm_1'],'coverage_chunk_count':3}}
        hashes=library.clothing_chunk_counts(catalog)
        self.assertEqual(hashes['7'],3)
        self.assertEqual(hashes[garments.fnv_path(catalog['7']['mesh'])],3)

    def test_torso_clothing_envelope_ignores_shell_without_deleting_skin(self):
        vertices=np.array([[-.2,.8,-.15],[.2,.8,-.15],[.2,1.5,-.15],[-.2,1.5,-.15],
                           [-.2,.8,.15],[.2,.8,.15],[.2,1.5,.15],[-.2,1.5,.15],
                           [-.2,.8,.15],[.2,.8,.15],[0,1.4,.27]])
        tri=np.array([[0,1,2],[0,2,3],[4,6,5],[4,7,6],[0,4,5],[0,5,1],
                      [3,2,6],[3,6,7],[0,3,7],[0,7,4],[1,5,6],[1,6,2],[8,9,10]])
        uv=np.zeros((len(vertices),2));uv[8:]=.5
        # Section boundaries duplicate vertices; UV-separated fur roots can
        # coincide with skin positions and must still remain a different patch.
        vertices,uv=vertices[tri].reshape(-1,3),uv[tri].reshape(-1,2)
        tri=np.arange(len(vertices)).reshape(-1,3);before=vertices.copy();indices=tri.copy()
        anchors={'Hips':[0,1,0],'Neck':[0,1.5,0],'Spine3':[0,1.3,0],
                 'LeftArm':[-.2,1.45,0],'RightArm':[.2,1.45,0]}
        result=garments.torso_skin_surface(vertices,tri,uv,np.arange(len(tri)),anchors)
        np.testing.assert_array_equal(result,np.arange(12))
        np.testing.assert_array_equal(vertices,before);np.testing.assert_array_equal(tri,indices)

    def test_torso_envelope_keeps_ambiguous_front_and_back_skin_patches(self):
        vertices=np.array([[-.2,.8,-.15],[.2,.8,-.15],[0,1.5,-.15],
                           [-.2,.8,.15],[.2,.8,.15],[0,1.5,.15]])
        tri=np.array([[0,1,2],[3,4,5]]);uv=np.zeros((len(vertices),2))
        anchors={'Hips':[0,1,0],'Neck':[0,1.5,0],'Spine3':[0,1.3,0],
                 'LeftArm':[-.2,1.45,0],'RightArm':[.2,1.45,0]}
        np.testing.assert_array_equal(garments.torso_skin_surface(vertices,tri,uv,np.arange(2),anchors),np.arange(2))

    def test_clothing_coverage_requires_surface_interior_and_maps_both_paths(self):
        writer=bridge.GLB();writer.doc['nodes']=[{'mesh':0}];writer.doc['scenes'][0]['nodes']=[0]
        p=np.array([[-1.,-1.,.02],[1.,-1.,.02],[1.,1.,.02],[-1.,1.,.02]])
        writer.doc['meshes']=[{'primitives':[{'attributes':{'POSITION':writer.array(p,'VEC3')},
                                             'indices':writer.array(np.array([0,1,2,0,2,3]),'SCALAR',5123)}]}]
        coverage=object.__new__(garments.ClothingCoverage);coverage.target_world={}
        coverage.sections={'torso_middle':(np.array([[0.,0.,0.]]),np.array([[0.,0.,1.]]),np.array([1.])),
                           'torso_upper':(np.array([[0.,1.08,0.]]),np.array([[0.,0.,1.]]),np.array([1.]))}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'shirt.glb';writer.save(path)
            result=coverage.garment(path,r'base\torso\shirt.mesh')
        self.assertEqual(result['regions'],['torso_middle'])
        catalog={'1':{'source':r'base\torso\shirt.mesh','mesh':r'avatarbridge\shirt.mesh','hide_regions':result['regions']}}
        hashes=library.clothing_coverage_hashes(catalog)
        self.assertEqual(hashes['1'],['torso_middle'])
        self.assertEqual(hashes[garments.fnv_path(catalog['1']['mesh'])],['torso_middle'])

    def test_body_sections_preserve_exposed_hands_feet_and_neck(self):
        anchors={name:np.array(value,dtype=float) for name,value in {
          'Hips':[0,1,0],'Neck':[0,1.6,0],'Spine1':[0,1.2,0],'Spine3':[0,1.4,0],
          'LeftArm':[-.2,1.5,0],'RightArm':[.2,1.5,0],
          'LeftHand':[-.8,1.5,0],'RightHand':[.8,1.5,0],
          'LeftUpLeg':[-.1,1,0],'RightUpLeg':[.1,1,0],
          'LeftFoot':[-.1,.1,0],'RightFoot':[.1,.1,0]}.items()}
        centers=np.array([[.9,1.5,0],[.1,.1,-.2],[0,1.8,0],[0,1.35,-.1],[.15,.8,0],[.4,1.5,0]])
        vertices=np.repeat(centers,3,axis=0);tri=np.arange(len(vertices)).reshape(-1,3)
        regions=geometry.body_regions(vertices,tri,anchors)
        self.assertEqual(regions.tolist(),['keep','keep','keep','torso_middle','rleg_0','rarm_0'])
        # A triangle straddling the first shorts section stays in the next
        # section, preserving overlap instead of leaving a jagged hole.
        vertices=np.array([[.1,.8,0],[.12,.8,0],[.11,.7,0]])
        self.assertEqual(geometry.body_regions(vertices,np.array([[0,1,2]]),anchors).tolist(),['rleg_1'])

    def test_support_morph_cannot_shrink_a_fitted_garment_through_body(self):
        writer=bridge.GLB();writer.doc['nodes']=[{'name':'Hips'},{'mesh':0,'skin':0}]
        writer.doc['scenes'][0]['nodes']=[0,1]
        writer.doc['skins']=[{'joints':[0],'inverseBindMatrices':writer.array(np.eye(4).reshape(1,16),'MAT4')}]
        positions=np.array([[0,0,0],[.02,0,0],[0,.02,0]],dtype=float)
        attributes={'POSITION':writer.array(positions,'VEC3'),
                    'JOINTS_0':writer.array(np.zeros((3,4)),'VEC4',5123),
                    'WEIGHTS_0':writer.array(np.tile([1.,0,0,0],(3,1)),'VEC4')}
        writer.doc['meshes']=[{'primitives':[{'attributes':attributes,'indices':writer.array(np.arange(3),'SCALAR',5123),
                         'targets':[{'POSITION':writer.array(np.tile([0,0,-.04],(3,1)),'VEC3')}]}]}]
        class Surface:
            target_world={'Hips':np.eye(4)}
            joint_nodes=[{'name':'Hips','translation':[0,1.,0]}]
            def garment_volume(self,points,labels,clearance,fit=True):
                result=points.copy();result[:,2]=np.maximum(result[:,2],clearance)
                return result,{'moved_vertices':int((result[:,2]!=points[:,2]).sum()),'max_displacement':float(np.abs(result-points).max())}
            def layered_hips(self,points,labels):
                result=points.copy();result[:,2]=np.maximum(result[:,2],.052);return result
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.glb';dest=Path(folder)/'fitted.glb';writer.save(source)
            garments.fit_glb(source,Surface(),dest,.01)
            doc,data=cp77.load_glb(dest);p=doc['meshes'][0]['primitives'][0]
            base=bridge.accessor_array(doc,data,p['attributes']['POSITION']);delta=bridge.accessor_array(doc,data,p['targets'][0]['POSITION'])
            self.assertGreaterEqual(float((base+delta)[:,2].min()),.00999)
            self.assertEqual(len(p['targets']),1)
            for role in ['outer','inner']:
                garments.fit_glb(source,Surface(),dest,.01,top_role=role)
                d,data=cp77.load_glb(dest);p=d['meshes'][0]['primitives'][0]
                base=bridge.accessor_array(d,data,p['attributes']['POSITION']);delta=bridge.accessor_array(d,data,p['targets'][0]['POSITION'])
                self.assertGreaterEqual(float(base[:,2].min()),.05199)
                if role=='outer':self.assertGreaterEqual(float((base+delta)[:,2].min()),.05199)
                else:self.assertAlmostEqual(float((base+delta)[:,2].min()),.01,places=5)

    def test_clothed_pelvis_patch_keeps_thighs_and_upper_body(self):
        anchors={'Hips':np.array([0.,1.,0.]),'LeftUpLeg':np.array([-.1,1.,0.]),'RightUpLeg':np.array([.1,1.,0.])}
        centers=np.array([[0,.88,-.1],[.17,.88,-.1],[0,1.1,-.1],[0,.88,.1],[0,.5,-.1]])
        points=np.repeat(centers,3,axis=0);tri=np.arange(15).reshape(-1,3)
        np.testing.assert_array_equal(geometry.covered_pelvis_triangles(points,tri,anchors),[True,False,False,False,False])
        garments_map={'1':{'source':r'base\legs\l1_test.mesh','mesh':r'avatarbridge\pants.mesh'},
                      '2':{'source':r'base\legs\shadow_meshes\l1_test_shadow.mesh','mesh':r'avatarbridge\shadow.mesh'}}
        self.assertEqual(set(library.lower_garment_hashes(garments_map)),{'1',garments.fnv_path(garments_map['1']['mesh'])})

    def test_original_flattening_shapes_preserve_all_skin_islands_and_indices(self):
        writer=bridge.GLB();positions=np.zeros((9,3),dtype=np.float32)
        accessor=writer.array(positions,'VEC3');primitives=[]
        for start in [0,3,6]:primitives.append({'attributes':{'POSITION':accessor},'indices':writer.array(np.arange(start,start+3),'SCALAR',5123)})
        flat=np.zeros((9,3));flat[3:6,2]=.02
        blink=np.zeros((9,3));blink[6:,1]=.01
        with patch.object(facial,'unity_deltas',return_value={'backflat':flat,'blink':blink}):
            names=facial.add_unity_targets(b'',9,writer,primitives)
        self.assertEqual(names,['blink']);self.assertEqual(len(primitives),3)
        self.assertEqual(writer.doc['extras']['removed_original_fur_triangles'],0)
        for i,primitive in enumerate(primitives):
            np.testing.assert_array_equal(bridge.accessor_array(writer.doc,writer.bin,primitive['indices']).reshape(-1),np.arange(i*3,i*3+3))
        actual=bridge.accessor_array(writer.doc,writer.bin,primitives[0]['attributes']['POSITION'])
        np.testing.assert_allclose(actual,flat)

    def test_unity_facial_frames_are_selected_mirrored_and_range_checked(self):
        source=b'''  m_Shapes:
    vertices:
    - vertex: {x: 0.1, y: 0.2, z: -0.3}
      normal: {x: 0, y: 0, z: 0}
      tangent: {x: 0, y: 0, z: 0}
      index: 1
    shapes:
    - firstVertex: 0
      vertexCount: 1
    channels:
    - name: Blink
      frameIndex: 0
      frameCount: 1
    fullWeights: [100]
  m_BindPose: []
'''
        result=facial.unity_deltas(source,3)
        self.assertEqual(set(result),{'blink'})
        np.testing.assert_allclose(result['blink'],[[0,0,0],[-.1,.2,-.3],[0,0,0]])
        with self.assertRaisesRegex(ValueError,'fuori intervallo'):
            facial.unity_deltas(source.replace(b'index: 1',b'index: 3'),3)
        with self.assertRaisesRegex(ValueError,'incompleto'):
            facial.unity_deltas(source.replace(b'vertexCount: 1',b'vertexCount: 2'),3)
        self.assertEqual(facial.unity_deltas(source.replace(b'name: Blink',b'name: ExtraClothing'),3),{})

    def test_dorsal_crest_removal_preserves_whole_head_tail_and_side_islands(self):
        anchors={'Hips':[0,1,0],'Chest':[0,1.4,0],'Neck':[0,1.6,0],'Head':[0,1.7,0],
                 'Left shoulder':[-.2,1.5,0],'Right shoulder':[.2,1.5,0]}
        points=np.array([[0,1.5,.1],[.01,1.45,.3],[-.01,1.4,.1],
                         [0,1.9,.1],[.01,1.85,.3],[-.01,1.8,.1],
                         [0,.9,.1],[.01,.8,.3],[-.01,.7,.1],
                         [.3,1.5,.1],[.31,1.45,.3],[.29,1.4,.1]])
        tri=np.arange(12).reshape(-1,3)
        np.testing.assert_array_equal(geometry.dorsal_crest_triangles(points,tri,anchors),[False,True,True,True])
        np.testing.assert_array_equal(geometry.dorsal_crest_triangles(points,tri,{}),[True]*4)

    def test_footwear_hides_source_and_refitted_paths_without_hiding_torso(self):
        shoes={'10':{'source':r'base\characters\garment\feet\s1_test.mesh','mesh':r'avatarbridge\test\shoes.mesh'},
               '20':{'source':r'base\characters\garment\torso\t1_test.mesh','mesh':r'avatarbridge\test\shirt.mesh'}}
        hidden=library.footwear_hashes(shoes)
        self.assertEqual(set(hidden),{'10',garments.fnv_path(shoes['10']['mesh'])})
        self.assertEqual(facial.recipes([]),[('neutral','Neutro',{})])
        self.assertEqual([r[0] for r in facial.recipes(['blink','smile'])],['neutral','smile'])

    def test_parallel_garment_import_restores_settings_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);settings=root/'appsettings.json'
            original=json.dumps({'GltfImportArgs':{'ImportGarmentSupport':False}})
            settings.write_text(original)
            calls=[]
            def command(*args):
                self.assertTrue(json.loads(settings.read_text())['GltfImportArgs']['ImportGarmentSupport'])
                calls.append(args)
                if str(args[1])=='0.glb':raise ValueError('failed native import')
            with patch.object(cp77,'cli',return_value=root/'WolvenKit.CLI.exe'),patch.object(cp77,'command',side_effect=command):
                with self.assertRaisesRegex(ValueError,'failed native import'):
                    garments.import_refits([Path(str(i)+'.glb') for i in range(17)],root)
            self.assertEqual(settings.read_text(),original)
            self.assertEqual(len(calls),2)

    def test_arm_cage_covers_both_sides_without_pulling_free_panels(self):
        envelope=object.__new__(garments.Envelope);angles=np.linspace(0,2*np.pi,32,endpoint=False)
        envelope.vertices=np.array([[x,1.4+.1*np.cos(a),.1*np.sin(a)] for x in [0.,.6] for a in angles])
        faces=[]
        for i in range(32):
            j=(i+1)%32;faces.extend([[i,j,i+32],[j,j+32,i+32]])
        envelope.triangles=np.array(faces);ids=np.arange(len(faces))
        envelope.groups={'all':(ids,None),'larm':(ids,None)}
        envelope.joint_nodes=[{'name':'Left'+name,'translation':[x,1.4,0.]} for name,x in [('Arm',0.),('ForeArm',.3),('Hand',.6)]]
        points=np.array([[.3,1.4,.02],[.3,1.38,0.],[.3,1.4,.3],[1.,1.4,0.]])
        result=garments.RadialCage(envelope,'larm').project(points,.035)
        self.assertGreater(result[0,2],.12)
        self.assertLess(result[1,1],1.28)
        np.testing.assert_allclose(result[2:],points[2:])
        loose=np.array([[.3,1.4,.18],[.3,1.4,.5],[1.,1.4,.18]])
        fitted=garments.RadialCage(envelope,'larm').project(loose,.006,fit=True)
        self.assertLess(fitted[0,2],.12)
        self.assertGreater(fitted[0,2],.10)
        np.testing.assert_allclose(fitted[1:],loose[1:])

    def test_garment_bind_pose_is_normalized_and_can_be_restored(self):
        writer=bridge.GLB();source=np.tile(np.eye(4),(2,1,1));source[0,0,3]=.2;source[1,0,3]=.25
        writer.doc['nodes']=[{'name':'LeftFoot'},{'name':'dyng_boot'}]
        writer.doc['skins']=[{'joints':[0,1],'inverseBindMatrices':writer.array(np.linalg.inv(source).transpose(0,2,1).reshape(2,16),'MAT4')}]
        transforms=garments.pose_transforms(writer.doc,writer.bin,{'LeftFoot':np.eye(4)})
        points=np.array([[.3,0.,0.],[.35,0.,0.]])
        normalized,matrices=garments.posed(points,np.eye(2),transforms)
        np.testing.assert_allclose(normalized,[[.1,0,0],[.15,0,0]],atol=1e-7)
        inverse=np.linalg.inv(matrices)
        restored=np.einsum('nij,nj->ni',inverse[:,:3,:3],normalized)+inverse[:,:3,3]
        np.testing.assert_allclose(restored,points,atol=1e-7)
        writer.doc['nodes'][0]['name']='shadow_LeftFoot'
        shadow=garments.pose_transforms(writer.doc,writer.bin,{'LeftFoot':np.eye(4)})
        np.testing.assert_allclose(shadow,transforms)
        self.assertEqual(garments.group('shadow_LeftForeArm'),'larm')

    def test_native_muscle_bones_keep_sleeves_on_the_correct_limb(self):
        for name in ['l_biceps_A_mscl_JNT','l_triceps_mscl_JNT','l_deltoid_front_bot_out_JNT','l_SHL_0_JNT']:
            self.assertEqual(garments.group(name),'larm')
        self.assertEqual(garments.group('r_knee_mscl_JNT'),'rleg')
        self.assertEqual(garments.group('l_chest_front_A_bot_out_JNT'),'torso')

    def test_refit_preserves_cloth_rig_and_reindexes_subdivided_garment_support(self):
        writer=bridge.GLB();points=np.array([[0.,0.,0.],[.2,0.,0.],[0.,.2,0.]])
        writer.doc['nodes']=[{'name':n} for n in ['Hips','Spine','LeftArm','RightArm','dyng_coat']]
        writer.doc['skins']=[{'joints':list(range(5)),'inverseBindMatrices':writer.array(np.tile(np.eye(4).reshape(1,16),(5,1)),'MAT4')}]
        attrs={'POSITION':writer.array(points,'VEC3'),
               'NORMAL':writer.array(np.tile([0.,0.,1.],(3,1)),'VEC3'),
               'JOINTS_0':writer.array(np.tile([4,0,0,0],(3,1)),'VEC4',5123),
               'WEIGHTS_0':writer.array(np.tile([1.,0,0,0],(3,1)),'VEC4')}
        delta=np.tile([0.,0.,-.015],(3,1))
        writer.doc['meshes']=[{'extras':{'targetNames':['GarmentSupport']},'primitives':[{'attributes':attrs,
            'indices':writer.array(np.array([0,1,2]),'SCALAR',5123),
            'targets':[{'POSITION':writer.array(delta,'VEC3')}]}]}]
        writer.doc['nodes'].append({'name':'submesh_00_LOD_1','mesh':0,'skin':0});writer.doc['scenes']=[{'nodes':list(range(6))}]
        class FlatEnvelope:
            def project(self,p,labels,margin):return p+np.array([0.,0.,.02]),{'moved_vertices':len(p),'max_displacement':.02}
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.glb';target=Path(folder)/'target.glb';writer.save(source)
            info=garments.fit_glb(source,FlatEnvelope(),target)
            doc,data=cp77.load_glb(target);p=doc['meshes'][0]['primitives'][0];a=p['attributes']
            count=doc['accessors'][a['POSITION']]['count']
            self.assertEqual(count,6)
            self.assertEqual(doc['accessors'][p['targets'][0]['POSITION']]['count'],count)
            np.testing.assert_allclose(bridge.accessor_array(doc,data,p['targets'][0]['POSITION']),np.tile([0,0,-.015],(count,1)))
            self.assertEqual([doc['nodes'][i]['name'] for i in doc['skins'][0]['joints']],['Hips','Spine','LeftArm','RightArm','dyng_coat'])
            np.testing.assert_array_equal(bridge.accessor_array(doc,data,a['JOINTS_0'])[:,0],4)
            self.assertEqual(info['morph_targets'],1)

    def test_garment_relaxation_welds_seams_and_preserves_constant_motion(self):
        points=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.],[0.,0.,0.],[-1.,0.,0.],[0.,-1.,0.]])
        original=points.copy();triangles=np.array([[0,1,2],[3,4,5]])
        relax=garments.seam_relaxer(points,triangles)
        field=np.zeros_like(points);field[0,2]=.3;field[3,2]=-.1
        result=relax(field)
        np.testing.assert_allclose(result[0],result[3])
        self.assertLess(float(np.abs(result[:,2]).max()),.3)
        np.testing.assert_allclose(relax(np.tile([.2,.3,.4],(6,1))),np.tile([.2,.3,.4],(6,1)))
        np.testing.assert_array_equal(points,original)

    def test_lower_body_cage_expands_inside_clothing_and_keeps_clear_points(self):
        envelope=object.__new__(garments.Envelope)
        envelope.joint_nodes=[{'name':name,'translation':position} for name,position in {
            'Hips':[0,.95,0],'LeftUpLeg':[.15,.9,0],'LeftLeg':[.15,.5,0],'LeftFoot':[.15,.1,0],
            'RightUpLeg':[-.15,.9,0],'RightLeg':[-.15,.5,0],'RightFoot':[-.15,.1,0]}.items()]
        points=[];faces=[];angles=np.linspace(0,2*np.pi,32,endpoint=False)
        for x,lower,upper,radius in [(0,.8,1.3,.32),(.15,0,.95,.18),(-.15,0,.95,.18)]:
            start=len(points)
            for y in [lower,upper]:points.extend([[x+radius*np.cos(a),y,radius*np.sin(a)] for a in angles])
            for i in range(32):
                j=(i+1)%32;faces.extend([[start+i,start+j,start+i+32],[start+j,start+j+32,start+i+32]])
        envelope.vertices=np.array(points);envelope.triangles=np.array(faces)
        cage=garments.LowerBodyCage(envelope)
        source=np.array([[.15,.5,.05],[-.15,.5,.05],[1.,.5,0],[0,2.,.1]])
        result=cage.project(source,.035)
        self.assertGreater(result[0,2],.18)
        np.testing.assert_allclose(result[0]*[-1,1,1],result[1],atol=.002)
        np.testing.assert_allclose(result[2:],source[2:])
        # REDengine's left thigh is on negative X. Labels must not imply an axis.
        for node in envelope.joint_nodes:node['translation'][0]*=-1
        mirrored=garments.LowerBodyCage(envelope).project(source,.035)
        np.testing.assert_allclose(mirrored,result,atol=.002)
        # The bottom of a shoe needs the whole paw footprint, even below the
        # first non-degenerate body slice; a narrow human sole cannot remain.
        sole=cage.project(np.array([[.15,-.01,.05]]),.035,footwear=True)
        self.assertGreater(sole[0,2],.18)

    def test_tail_graph_has_variable_container_and_resolvable_node_references(self):
        captured=[]
        targets={'Root':np.eye(4),'Hips':np.eye(4),'dyng_avatar_tail_00':np.eye(4),'dyng_avatar_tail_01':np.eye(4)}
        targets['dyng_avatar_tail_01'][0,3]=.2
        with patch.object(cp77,'deserialize',side_effect=lambda doc,*args:captured.append(doc)):
            physics.resources({'Data':{}},targets,['dyng_avatar_tail_00','dyng_avatar_tail_01'],Path('synthetic'))
        graph=captured[1]['Data']['RootChunk']
        self.assertEqual(graph['variables']['Data']['$type'],'animAnimVariableContainer')
        ids=set();references=set()
        def visit(value):
            if isinstance(value,dict):
                if 'HandleId' in value:ids.add(value['HandleId'])
                if 'HandleRefId' in value:references.add(value['HandleRefId'])
                for child in value.values():visit(child)
            elif isinstance(value,list):
                for child in value:visit(child)
        visit(graph)
        self.assertTrue(references.issubset(ids))

    def test_back_fur_follows_nearest_skin_weights_without_changing_shape(self):
        writer=bridge.GLB();writer.doc['skins']=[{'joints':[0,1,2,3]}]
        writer.doc['materials']=[{'name':'Body skin'},{'name':'Feathers'}]
        primitives=[]
        for material,z,bone in [(0,0.,0),(1,.02,1)]:
            a={'POSITION':writer.array(np.array([[0.,0.,z],[1.,0.,z],[0.,1.,z]]),'VEC3'),
               'JOINTS_0':writer.array(np.tile([bone,0,0,0],(3,1)),'VEC4',5123),
               'WEIGHTS_0':writer.array(np.tile([1.,0,0,0],(3,1)),'VEC4')}
            primitives.append({'material':material,'attributes':a,'indices':writer.array(np.array([0,1,2]),'SCALAR',5123)})
        writer.doc['meshes']=[{'primitives':primitives}]
        original=bytes(writer.bin)
        result,info=geometry.accessory_weights(writer.doc,writer.bin)
        joints,weights=result[id(primitives[1])]
        self.assertEqual(info['islands'],1)
        np.testing.assert_array_equal(joints[:,0],0)
        np.testing.assert_allclose(weights[:,0],1)
        self.assertEqual(bytes(writer.bin),original)

    def test_garment_projection_moves_inside_vertices_outside_and_keeps_clear_vertices(self):
        envelope=object.__new__(garments.Envelope)
        envelope.vertices=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
        envelope.normals=np.tile([0.,0.,1.],(3,1))
        envelope.triangles=np.array([[0,1,2]])
        envelope.groups={'all':(np.array([0]),cKDTree([[1/3,1/3,0.]]))}
        points=np.array([[.2,.2,-.1],[.3,.3,.1],[.3,.3,-2.]])
        fitted,stats=envelope.project(points,np.array(['torso']*3),.014)
        np.testing.assert_allclose(fitted[0],[.2,.2,.014])
        np.testing.assert_array_equal(fitted[1:],points[1:])
        self.assertEqual(stats['moved_vertices'],1)

    def test_garment_nearest_triangle_handles_edges_and_not_only_vertex_distance(self):
        triangles=np.array([[[[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]]]])
        closest,_=garments.nearest_triangle(np.array([[.5,-.2,.1]]),triangles)
        np.testing.assert_allclose(closest,[[.5,0.,0.]])

    def test_garment_uses_interpolated_avatar_skin_weights(self):
        envelope=object.__new__(garments.Envelope)
        envelope.vertices=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
        envelope.triangles=np.array([[0,1,2]])
        envelope.groups={'all':(np.array([0]),cKDTree([[1/3,1/3,0.]]))}
        envelope.joint_names=['Hips','LeftUpLeg','RightUpLeg','Spine']
        envelope.skin_weights=np.array([[1.,0,0,0],[0.,1,0,0],[0.,1,0,0]])
        joints,weights=envelope.weights(np.array([[.25,.25,.04]]),np.array(['torso']))
        result=np.zeros(4);result[joints[0]]=weights[0]
        np.testing.assert_allclose(result,[.5,.5,0.,0.])

    def test_tail_chain_excludes_clothes_and_frame_stays_orthonormal(self):
        self.assertEqual(physics.tail_chain(['Tail2','tailcoat','Tail_Rt','Tail0','Tail1']),['Tail_Rt','Tail0','Tail1','Tail2'])
        for direction in [np.array([1.,0,0]),np.array([0.,1.,0]),np.array([.3,-.4,.7])]:
            frame=physics.frame_x(direction)
            np.testing.assert_allclose(frame.T@frame,np.eye(3),atol=1e-8)
            self.assertAlmostEqual(np.linalg.det(frame),1.)

    def test_native_surface_patch_replaces_matching_id_and_preserves_other_gender(self):
        def appearance(gender):
            return {"HandleId":"0","Data":{"name":cp77.name(gender+"_default"),"components":[{
                "$type":"entSkinnedMeshComponent","name":cp77.name("t0_000_"+gender+"_base__full"),
                "id":"42","mesh":cp77.ref("human.mesh"),"isEnabled":1,"chunkMask":"7"}],"compiledData":{"old":True}}}
        original={"Data":{"RootChunk":{"appearances":[appearance("pma"),appearance("pwa")]}}}
        result=cp77.surface_patch(original,"pma")["Data"]["RootChunk"]["appearances"]
        self.assertEqual(len(result),1)
        c=result[0]["Data"]["components"][0]
        self.assertEqual((c["id"],c["isEnabled"],c["chunkMask"]),("42",0,"0"))
        self.assertNotIn("mesh",c)
        self.assertEqual(original["Data"]["RootChunk"]["appearances"][0]["Data"]["components"][0]["isEnabled"],1)

    def test_library_variant_resources_and_component_ids_are_distinct(self):
        report={"name":"Test","category":"Species","gender":"male","selected_base":"Body.asset"}
        first=library.catalog_entry(Path("avatar_a"),report);second=library.catalog_entry(Path("avatar_b"),report)
        a=library.body_component(first,"body",r"avatarbridge\a",True)
        b=library.body_component(second,"body",r"avatarbridge\b",False)
        self.assertNotEqual(a["id"],b["id"])
        self.assertNotEqual(a["name"],b["name"])
        self.assertEqual((a["isEnabled"],b["isEnabled"]),(1,0))
        self.assertEqual(b["mesh"]["DepotPath"]["$value"],r"avatarbridge\b\body.mesh")
        literal=library.lua_literal({"name":"]==] ]=] ]] malicious"})
        self.assertTrue(literal.startswith("[===["))

    def test_tangents_follow_uvs_and_are_orthogonal_to_normals(self):
        p=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
        n=np.tile([0.,0.,1.],(3,1));uv=p[:,:2]
        result=cp77.tangents(p,n,uv,np.array([0,1,2]))
        np.testing.assert_allclose(result,np.tile([1.,0.,0.,1.],(3,1)),atol=1e-6)
        np.testing.assert_allclose((result[:,:3]*n).sum(axis=1),0,atol=1e-6)

    def test_joint_transform_roundtrip_includes_half_turns(self):
        for axis in [np.eye(3)[i] for i in range(3)]:
            for angle in [0,.7,np.pi,4.2]:
                q=np.r_[axis*np.sin(angle/2),np.cos(angle/2)]
                matrix=bridge.node_matrix({"rotation":q,"translation":[.1,1.5,-.3]})
                np.testing.assert_allclose(bridge.node_matrix(cp77.trs(matrix)),matrix,atol=1e-6)

    def test_bone_alias_preserves_hands_and_original_eyes(self):
        self.assertEqual(cp77.bone_alias("Left wrist"),"LeftHand")
        self.assertEqual(cp77.bone_alias("Right leg"),"RightUpLeg")
        self.assertEqual(cp77.bone_alias("ThumbFinger3_L"),"LeftHandThumb2")
        self.assertEqual(cp77.bone_alias("Eye_R"),"Head")
        with self.assertRaises(ValueError):cp77.bone_alias("unknown_whisker")

    def fixture(self,root):
        game=root/"game";data=root/"data"
        for relative in ["bin/x64/Cyberpunk2077.exe","bin/x64/plugins/cyber_engine_tweaks.asi",
                         "red4ext/plugins/ArchiveXL/ArchiveXL.dll","red4ext/plugins/Codeware/Codeware.dll",
                         "bin/x64/winmm.dll","bin/x64/version.dll","red4ext/RED4ext.dll",
                         "engine/config/base/scripts.ini","engine/tools/scc.exe","engine/tools/scc_lib.dll"]:
            p=game/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b"test executable placeholder")
        job=data/"jobs/test";package=job/"package";package.mkdir(parents=True)
        archive=package/(cp77.MOD+".archive");archive.write_bytes(b"RDAR synthetic archive")
        (package/(cp77.MOD+".archive.xl")).write_text("resource: {}")
        (package/"init.lua").write_text("-- local synthetic script")
        (job/"build.json").write_text(json.dumps({"stage":"compiled","package":str(package),
            "sha256":hashlib.sha256(archive.read_bytes()).hexdigest()}))
        return game,data,job,package

    def test_disabled_loader_blocks_install_before_existing_mods_change(self):
        with tempfile.TemporaryDirectory() as temp:
            game,data,job,_=self.fixture(Path(temp))
            (game/"bin/x64/winmm.dll").unlink()
            mods=game/"archive/pc/mod";mods.mkdir(parents=True)
            original=mods/"ExoticsCO-ExoticV.archive";original.write_bytes(b"original")
            with patch.object(bridge,"DATA",data),patch.object(cp77,"running_game",return_value=False):
                with self.assertRaisesRegex(ValueError,"RED4ext loader"):
                    cp77.install(job,game)
            self.assertEqual(original.read_bytes(),b"original")
            self.assertFalse((data/"installations").exists())
            self.assertFalse((mods/(cp77.MOD+".archive")).exists())

    def test_patch_25_known_archivexl_crash_blocks_install(self):
        with tempfile.TemporaryDirectory() as temp:
            game,data,job,_=self.fixture(Path(temp))
            def versions(path):
                if path.name=="Cyberpunk2077.exe":return {"ProductVersion":"2.5"}
                if path.name=="ArchiveXL.dll":return {"FileVersion":"1.27.5.2610192005"}
                if path.name=="RED4ext.dll":return {"FileVersion":"1.30.0"}
                return {}
            with patch.object(bridge,"DATA",data),patch.object(cp77,"running_game",return_value=False),patch.object(bridge,"windows_version_info",side_effect=versions):
                with self.assertRaisesRegex(ValueError,"ArchiveXL 1.27.5"):
                    cp77.install(job,game)
            self.assertFalse((data/"installations").exists())

    def test_other_framework_versions_do_not_inherit_known_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            game,_,_,_=self.fixture(Path(temp))
            for game_version,archive_version,extender_version in [("2.31","1.27.5.2610192005","1.30.0"),
                    ("2.5","1.27.6","1.30.0"),("2.5","1.27.5.2610192005","1.31.0")]:
                def versions(path):
                    if path.name=="RED4ext.dll":return {"FileVersion":extender_version}
                    return {"ProductVersion":game_version} if path.name=="Cyberpunk2077.exe" else {"FileVersion":archive_version}
                with self.subTest(game=game_version,archive=archive_version),patch.object(bridge,"windows_version_info",side_effect=versions):
                    status=bridge.runtime_status(game)
                    self.assertTrue(status["ready"])
                    self.assertFalse(status["runtime_verified"])

    def test_install_remove_restores_only_conflicting_v_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            game,data,job,_=self.fixture(Path(temp))
            mods=game/"archive/pc/mod";mods.mkdir(parents=True)
            (mods/"ExoticsCO-ExoticV.archive").write_bytes(b"original player mod")
            (mods/"ExoticsCO-CougarPanam.archive").write_bytes(b"do not modify NPC mod")
            with patch.object(bridge,"DATA",data),patch.object(cp77,"running_game",return_value=False):
                cp77.install(job,game)
                self.assertFalse((mods/"ExoticsCO-ExoticV.archive").exists())
                self.assertTrue((mods/(cp77.MOD+".archive")).is_file())
                cp77.uninstall(game)
                self.assertEqual((mods/"ExoticsCO-ExoticV.archive").read_bytes(),b"original player mod")
                self.assertEqual((mods/"ExoticsCO-CougarPanam.archive").read_bytes(),b"do not modify NPC mod")
                self.assertFalse((mods/(cp77.MOD+".archive")).exists())

    def test_install_failure_rolls_back_existing_mods(self):
        with tempfile.TemporaryDirectory() as temp:
            game,data,job,_=self.fixture(Path(temp))
            mods=game/"archive/pc/mod";mods.mkdir(parents=True)
            (mods/"ExoticsCO-ExoticV.archive").write_bytes(b"original")
            with patch.object(bridge,"DATA",data),patch.object(cp77,"running_game",return_value=False),patch.object(cp77.os,"replace",side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):cp77.install(job,game)
            self.assertEqual((mods/"ExoticsCO-ExoticV.archive").read_bytes(),b"original")
            self.assertFalse((mods/(cp77.MOD+".archive")).exists())

    def test_modified_installation_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            game,data,job,_=self.fixture(Path(temp))
            with patch.object(bridge,"DATA",data),patch.object(cp77,"running_game",return_value=False):
                cp77.install(job,game)
                archive=game/"archive/pc/mod"/(cp77.MOD+".archive")
                archive.write_bytes(b"user edits")
                with self.assertRaisesRegex(ValueError,"modificato"):cp77.uninstall(game)
                self.assertEqual(archive.read_bytes(),b"user edits")


if __name__=="__main__":unittest.main()
