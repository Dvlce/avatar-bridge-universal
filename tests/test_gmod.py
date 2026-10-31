import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import bridge
import gmod
from PIL import Image


class GmodTests(unittest.TestCase):
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
