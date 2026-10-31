import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import numpy as np
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import bridge
import gmod
from PIL import Image


class GmodTests(unittest.TestCase):
    def test_leg_segments_meet_at_the_same_knee_without_changing_width(self):
        forward=np.array([0.,-1.,0.])
        hip=np.array([4.,1.,40.]); knee=np.array([4.,0.,23.]); ankle=np.array([4.,3.,4.])
        target_hip=np.array([4.,0.,39.]); target_knee=np.array([4.,.4,21.]); target_ankle=np.array([4.,1.8,4.])
        thigh=gmod.segment_transform(hip,knee,target_hip,target_knee,forward)
        calf=gmod.segment_transform(knee,ankle,target_knee,target_ankle,forward)
        for source,destination in [(hip,target_hip),(knee,target_knee)]:
            np.testing.assert_allclose((thigh@np.r_[source,1])[:3],destination,atol=1e-10)
        for source,destination in [(knee,target_knee),(ankle,target_ankle)]:
            np.testing.assert_allclose((calf@np.r_[source,1])[:3],destination,atol=1e-10)
        self.assertAlmostEqual(np.linalg.norm(thigh[:3,:3]@np.array([1.,0.,0.])),1)

    def test_pelvis_field_preserves_cross_sections_and_hip_chest_connections(self):
        hips=np.array([0.,1.5,40.]); chest=np.array([0.,.1,55.])
        target_hips=np.array([0.,-.5,39.]); target_chest=np.array([0.,3.2,50.])
        tr=gmod.torso_transform(hips,chest,target_hips,target_chest)
        for source,destination in [(hips,target_hips),(chest,target_chest)]:
            np.testing.assert_allclose((tr@np.r_[source,1])[:3],destination,atol=1e-10)
        np.testing.assert_allclose(tr[:3,:3]@np.array([1.,0.,0.]),[1,0,0])
        np.testing.assert_allclose(tr[:3,:3]@np.array([0.,1.,0.]),[0,1,0])

    def test_anatomical_hip_field_meets_both_thigh_sockets(self):
        hips=np.array([0.,1.5,40.]);chest=np.array([0.,.1,55.])
        tr=gmod.torso_transform(hips,chest,np.array([0.,-.5,39.]),np.array([0.,3.,50.]),.8)
        for side in [-1,1]:
            source=hips+np.array([side*5.,0,0])
            expected=np.array([side*4.,-.5,39.])
            np.testing.assert_allclose((tr@np.r_[source,1])[:3],expected,atol=1e-10)

    def test_uv_seam_duplicates_keep_one_binding_in_bent_poses(self):
        p=np.array([[0.,0.,40.],[0.,0.,40.],[1.,0.,35.]])
        dense=np.array([[.6,.3,.1,0],[.1,.5,.1,.3],[0,0,1,0.]])
        points,joints,weights,groups,seams=gmod.shared_bindings(p,dense)
        self.assertEqual(seams,1);self.assertEqual(groups[0],groups[1])
        self.assertEqual(joints.shape[1],3)
        np.testing.assert_allclose(weights.sum(1),1)
        rotations=np.repeat(np.eye(4)[None,:,:],4,axis=0)
        rotations[1,:3,:3]=np.array([[1,0,0],[0,.5,-np.sqrt(.75)],[0,np.sqrt(.75),.5]])
        rotations[2,:3,3]=[0,3,-5]
        blend=(rotations[joints]*weights[:,:,None,None]).sum(1)
        posed=np.einsum('nij,nj->ni',blend[:,:3,:3],points)+blend[:,:3,3]
        np.testing.assert_array_equal(posed[groups[0]],posed[groups[1]])

    def test_tail_jiggle_axes_follow_the_chain_without_moving_bind_origins(self):
        bones=[]
        for i,position in enumerate([(0,0,40),(0,3,36),(0,10,35),(0,18,33)]):
            world=np.eye(4); world[:3,3]=position
            bones.append(dict(name='ValveBiped.Bip01_Pelvis' if i==0 else 'AvatarBridge.Tail'+str(i),
                              parent=i-1,world=world,local=world.copy()))
        positions=[b['world'][:3,3].copy() for b in bones]
        dynamic=gmod.tail_dynamics(bones)
        self.assertEqual([x['name'] for x in dynamic],['AvatarBridge.Tail2','AvatarBridge.Tail3'])
        for i in range(1,len(bones)):
            np.testing.assert_allclose(bones[i]['world'][:3,3],positions[i])
            np.testing.assert_allclose(bones[i-1]['world']@bones[i]['local'],bones[i]['world'],atol=1e-10)
            direction=positions[i+1]-positions[i] if i+1<len(bones) else positions[i]-positions[i-1]
            np.testing.assert_allclose(bones[i]['world'][:3,2],direction/np.linalg.norm(direction),atol=1e-10)
        qc='\n'.join(gmod.physics_qc(bones,dynamic))
        self.assertNotIn('$jigglebone "AvatarBridge.Tail1"',qc)
        self.assertEqual(qc.count('$jigglebone'),2)
        self.assertIn('angle_constraint 25',qc)

    def test_ragdoll_has_articulated_limbs_and_distinct_hands_and_feet(self):
        bones=[dict(name='ValveBiped.Bip01_'+name) for name in gmod.PHYSICS_PARTS]
        qc='\n'.join(gmod.physics_qc(bones,[]))
        self.assertEqual(len(gmod.PHYSICS_PARTS),15)
        for name in gmod.PHYSICS_PARTS[1:]:
            for axis in 'xyz': self.assertIn(f'$jointconstrain "ValveBiped.Bip01_{name}" {axis} limit',qc)
        self.assertIn('L_Forearm" z limit -149 4',qc)
        self.assertIn('R_Calf" z limit -10 126',qc)
        for name in ['L_Hand','R_Hand','L_Foot','R_Foot']:
            self.assertIn(name,gmod.PHYSICS_PARTS)

    def test_frames_are_rotations_even_when_axis_is_parallel_to_forward(self):
        for axis in [np.array([0.,-2.,0.]), np.array([0.,0.,3.]), np.array([2.,1.,.2])]:
            rotation = gmod.frame(axis,np.array([0.,-1.,0.]))
            np.testing.assert_allclose(rotation.T@rotation,np.eye(3),atol=1e-12)
            self.assertAlmostEqual(np.linalg.det(rotation),1.)
        with self.assertRaises(ValueError):
            gmod.frame(np.zeros(3),np.array([0.,-1.,0.]))

    def test_coincident_hips_spine_do_not_twist_an_aligned_humanoid(self):
        definitions = [('Hips','Pelvis',None,(0,0,40)),('Spine','Spine','Pelvis',(0,0,44)),
            ('Chest','Spine2','Spine',(0,0,52)),('Neck','Neck1','Spine2',(0,0,60)),('Head','Head1','Neck1',(0,0,64))]
        for side,letter,sign in [('Left','L',1),('Right','R',-1)]:
            definitions += [(side+' arm',letter+'_UpperArm','Spine2',(sign*8,0,57)),
                (side+' elbow',letter+'_Forearm',letter+'_UpperArm',(sign*15,0,50)),
                (side+' wrist',letter+'_Hand',letter+'_Forearm',(sign*22,0,43)),
                (side+' leg',letter+'_Thigh','Pelvis',(sign*4,0,40)),
                (side+' knee',letter+'_Calf',letter+'_Thigh',(sign*4,0,22)),
                (side+' ankle',letter+'_Foot',letter+'_Calf',(sign*4,0,4))]
        conversion=np.array([[1.,0.,0.],[0.,0.,-1.],[0.,1.,0.]])
        reference=[]; nodes=[]; indices={}
        for source,name,parent,position in definitions:
            index=len(reference); indices[name]=index
            world=np.eye(4); world[:3,3]=position
            parentindex=indices[parent] if parent else -1
            local=np.linalg.inv(reference[parentindex]['world'])@world if parent else world.copy()
            reference.append(dict(name='ValveBiped.Bip01_'+name,parent=parentindex,world=world,local=local))
            sourceposition=np.array([0.,0.,40.]) if source=='Spine' else np.array(position,dtype=float)
            nodes.append(dict(name=source,translation=(conversion.T@sourceposition/39.37007874).tolist()))
        doc={'nodes':nodes,'skins':[{'joints':list(range(len(nodes)))}]}
        _,remap,transforms,actual_conversion=gmod.skeleton(doc,b'',reference)
        np.testing.assert_array_equal(actual_conversion,conversion)
        for index,tr in enumerate(transforms):
            np.testing.assert_allclose(tr[:3,:3],np.eye(3),atol=1e-8,err_msg=nodes[index]['name'])
            self.assertAlmostEqual(tr[0,3],0,places=7)
        self.assertEqual(remap[0],0)

    def test_equivalent_game_paths_share_installation_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'GarrysMod'
            self.assertEqual(gmod.manifest_path(root),gmod.manifest_path(root/'garrysmod'/'..'))

    def test_vtf_header_and_pixels_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'texture.vtf'
            gmod.write_vtf(Image.new('RGBA',(3,5),(11,22,33,44)),path)
            raw = path.read_bytes()
            self.assertEqual(struct.unpack_from('<4sIIIHH',raw), (b'VTF\0',7,2,80,4,8))
            self.assertEqual(struct.unpack_from('<I',raw,52)[0],0)
            self.assertEqual(raw[56],1)
            self.assertEqual(len(raw),80+4*8*4)
            self.assertTrue(all(abs(a-b)<=1 for a,b in zip(raw[80:84],[11,22,33,44])))

    def test_aliases_keep_fingers_distinct_and_accessories_on_ancestors(self):
        self.assertEqual(gmod.bone_alias('Left elbow'),'ValveBiped.Bip01_L_Forearm')
        self.assertEqual(gmod.bone_alias('ThumbFinger3_R'),'ValveBiped.Bip01_R_Finger02')
        self.assertIsNone(gmod.bone_alias('Jaw'))
        self.assertIsNone(gmod.bone_alias('UnrecognizedBone'))

    def fixture(self, directory):
        root = Path(directory)/'GarrysMod'
        (root/'garrysmod').mkdir(parents=True)
        (root/'garrysmod/gameinfo.txt').write_text('test')
        addon = root/'garrysmod/addons/avatarbridge_test'
        (addon/'models').mkdir(parents=True)
        (addon/'models/test.mdl').write_bytes(b'original')
        manifest = gmod.manifest_path(root)
        manifest.parent.mkdir(parents=True,exist_ok=True)
        manifest.write_text(json.dumps({'addons':{'avatarbridge_test':{'files':{'models/test.mdl':hashlib.sha256(b'original').hexdigest()}}}}))
        return root,addon,manifest

    def test_remove_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge,'DATA',Path(directory)/'data'), patch.object(gmod,'require_closed'):
            root,addon,_ = self.fixture(directory)
            (addon/'my-notes.txt').write_text('keep')
            gmod.uninstall(root, 'avatar_test')
            self.assertFalse((addon/'models/test.mdl').exists())
            self.assertEqual((addon/'my-notes.txt').read_text(),'keep')

    def test_remove_refuses_changed_model_before_touching_anything(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge,'DATA',Path(directory)/'data'), patch.object(gmod,'require_closed'):
            root,addon,manifest = self.fixture(directory)
            before = manifest.read_bytes()
            (addon/'models/test.mdl').write_bytes(b'modified')
            with self.assertRaisesRegex(ValueError,'File modificato'):
                gmod.uninstall(root,'avatar_test')
            self.assertEqual((addon/'models/test.mdl').read_bytes(),b'modified')
            self.assertEqual(manifest.read_bytes(),before)

    def test_manifest_cannot_remove_file_outside_addon(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge,'DATA',Path(directory)/'data'), patch.object(gmod,'require_closed'):
            root,addon,manifest = self.fixture(directory)
            victim = root/'garrysmod/keep.txt'; victim.write_bytes(b'keep')
            manifest.write_text(json.dumps({'addons':{'avatarbridge_test':{'files':{'../../keep.txt':hashlib.sha256(b'keep').hexdigest()}}}}))
            with self.assertRaisesRegex(ValueError,'Manifest non valido'):
                gmod.uninstall(root,'avatar_test')
            self.assertEqual(victim.read_bytes(),b'keep')


if __name__=='__main__': unittest.main()
