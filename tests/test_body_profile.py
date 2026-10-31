from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import geometry
import garments
import library
import cp77


class BodyProfileTests(unittest.TestCase):
    def test_pelvis_extension_is_compact_and_mapping_stays_monotonic(self):
        anchors={'Hips':np.array([0.,1.,0.]),'LeftUpLeg':np.array([-.085,1.,0.]),
                 'RightUpLeg':np.array([.085,1.,0.])}
        heights=np.linspace(.55,1.10,200)
        positions=np.column_stack([np.zeros(200),heights,np.full(200,-.12)])
        normals=np.tile([0.,0.,-1.],(200,1))
        result,normal,audit=geometry.fit_pelvis_profile(positions,normals,np.ones(200),anchors)
        self.assertGreater(result[np.searchsorted(heights,.72),1],.84)
        np.testing.assert_array_equal(result[heights<=.62],positions[heights<=.62])
        self.assertTrue(np.all(np.diff(result[:,1])>0))
        np.testing.assert_array_equal(result[heights>=.895],positions[heights>=.895])
        np.testing.assert_allclose(np.linalg.norm(normal,axis=1),1)
        self.assertGreater(audit['moved_vertices'],0)

    def test_pelvis_profile_preserves_thighs_back_and_nonhip_surfaces(self):
        anchors={'Hips':np.array([0.,1.,0.]),'LeftUpLeg':np.array([-.085,1.,0.]),
                 'RightUpLeg':np.array([.085,1.,0.])}
        positions=np.array([[.14,.7,-.12],[0,.7,.12],[0,.5,-.12],[0,1.2,-.12]])
        normals=np.tile([0.,0.,-1.],(4,1))
        result,_,audit=geometry.fit_pelvis_profile(positions,normals,np.array([1,1,.1,1]),anchors)
        np.testing.assert_array_equal(result,positions)
        self.assertEqual(audit['moved_vertices'],0)

    def test_connected_groin_surface_does_not_fold_at_different_hip_weights(self):
        anchors={'Hips':np.array([0.,1.,0.]),'LeftUpLeg':np.array([-.085,1.,0.]),
                 'RightUpLeg':np.array([.085,1.,0.])}
        positions=np.tile([.01,.74,-.11],(3,1));normals=np.tile([0.,0.,-1.],(3,1))
        result,_,_=geometry.fit_pelvis_profile(positions,normals,np.array([.01,.55,.95]),anchors)
        np.testing.assert_array_equal(result,np.tile(result[0],(3,1)))
        self.assertGreater(result[0,1],.86)

    def test_tail_base_retains_spine_and_shared_boundary_binding(self):
        triangles=np.array([[0,1,2],[1,2,3]])
        joints=np.tile([0,1,2,0],(4,1))
        weights=np.array([[.4,.6,0,0],[.4,.59,.01,0],[.4,.59,.01,0],[0,.2,.8,0]])
        selected,mapped,count=cp77.tail_partition(triangles,joints,weights,[2],0)
        np.testing.assert_array_equal(selected,[False,True])
        self.assertEqual(count,2)
        np.testing.assert_array_equal(mapped[[1,2],2],0)
        # The spine is retained in the tail mesh instead of collapsing to Hips.
        names=cp77.part_skin_names('tail',['Hips','Spine1','tail'],['tail'],
            [{'joints':mapped,'weights':weights}])
        self.assertEqual(names,['Hips','Spine1','tail'])

    def test_tail_uv_duplicates_share_body_binding_in_bent_pose(self):
        names=['Hips','Spine1','tail','LeftUpLeg']
        body={'material':1,'positions':np.array([[0.,1.,.1]]),
              'joints':np.array([[0,1,3,0]]),'weights':np.array([[.3,.6,.1,0.]])}
        tail={'material':1,'positions':np.array([[0.,1.,.1]]),
              'joints':np.array([[0,1,2,3]]),'weights':np.array([[.3,.55,.05,.1]])}
        first_person={k:v.copy() if hasattr(v,'copy') else v for k,v in body.items()}
        count=cp77.stitch_tail_seams({'body':[body],'body_fpp':[first_person],'tail':[tail]},names,['tail'])
        self.assertEqual(count,3)
        transforms=np.tile(np.eye(4),(4,1,1));transforms[1,:3,3]=[0,.02,.10]
        def deform(p):
            point=p['positions'][0]
            return sum(weight*(transforms[j,:3,:3]@point+transforms[j,:3,3])
                       for j,weight in zip(p['joints'][0],p['weights'][0]))
        np.testing.assert_allclose(deform(body),deform(tail),atol=1e-8)
        np.testing.assert_allclose(deform(first_person),deform(tail),atol=1e-8)

    def test_open_david_jacket_never_hides_the_exposed_abdomen(self):
        source=r'base\characters\garment\player_equipment\torso\t2_161_jacket__edgerunners\t2_161_pma_jacket__edgerunners.mesh'
        result=garments.covered_regions({'torso_lower':1.0,'torso_middle':1.0,'hips':1.0,'larm_0':1.0},source)
        self.assertEqual(result,['larm_0'])
        # Reused compiled garments need the same correction, without a refit.
        hashes=library.clothing_coverage_hashes({'1':{'source':source,'mesh':r'avatarbridge\jacket.mesh',
                                                     'hide_regions':['torso_lower','larm_0']}})
        self.assertEqual(hashes['1'],['larm_0'])

    def test_compact_body_keeps_skin_at_pants_waist_and_crotch(self):
        catalog={'1':{'source':r'base\legs\pants.mesh','mesh':r'avatarbridge\pants.mesh',
                      'hide_regions':['pelvis','lleg_0','rleg_0']}}
        result=library.clothing_coverage_hashes(catalog,compact_pelvis=True)
        self.assertEqual(result['1'],['lleg_0','rleg_0'])
        self.assertEqual(library.clothing_coverage_hashes(catalog)['1'],['pelvis','lleg_0','rleg_0'])

    def test_pants_hide_enclosed_leg_skin_and_keep_waist_and_lower_back(self):
        catalog={'1':{'source':r'base\legs\pants.mesh','mesh':r'avatarbridge\pants.mesh',
                      'hide_regions':['pelvis','lleg_0','rleg_0','torso_lower']}}
        result=library.clothing_coverage_hashes(catalog,compact_pelvis=True,intact_skin=True)
        self.assertTrue(result)
        self.assertTrue(all(regions==['lleg_0','rleg_0'] for regions in result.values()))


if __name__=='__main__':unittest.main()
