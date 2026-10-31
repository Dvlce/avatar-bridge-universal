"""Synthetic fixtures only. No avatar or game content is committed to source."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import bridge


def tiny_base(root):
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype="<f4")
    normals = np.tile([0., 0., 1.], (3, 1)).astype("<f4")
    weights = np.tile([1., 0., 0., 0.], (3, 1)).astype("<f4")
    joints = np.zeros((3, 4), dtype="<u4")
    interleaved = bytearray()
    for i in range(3):
        interleaved.extend(positions[i].tobytes() + normals[i].tobytes() + weights[i].tobytes() + joints[i].tobytes())
    channels = [{"stream": 0, "offset": 0, "format": 0, "dimension": 0} for _ in range(14)]
    for i, offset, fmt, dim in [(0, 0, 0, 3), (1, 12, 0, 3), (12, 24, 0, 4), (13, 40, 10, 4)]:
        channels[i] = {"stream": 0, "offset": offset, "format": fmt, "dimension": dim}
    identity = {f"e{r}{c}": float(r == c) for r in range(4) for c in range(4)}
    mesh = {"m_Name": "Body", "m_VertexData": {"m_VertexCount": 3, "m_Channels": channels,
                                               "_typelessdata": interleaved.hex()},
            "m_BindPose": [identity], "m_IndexBuffer": struct.pack("<3H", 0, 1, 2).hex(),
            "m_SubMeshes": [{"topology": 0, "firstByte": 0, "indexCount": 3, "baseVertex": 0}]}
    (root / "Body.asset").write_text("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!43 &4300000\n" + yaml.safe_dump({"Mesh": mesh}, sort_keys=False))
    (root / "Body.asset.meta").write_text("guid: abcdef123456\n")
    prefab = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &1
GameObject:
  m_Name: Hips
--- !u!4 &2
Transform:
  m_GameObject: {fileID: 1}
  m_Father: {fileID: 0}
--- !u!1 &3
GameObject:
  m_Name: Body
--- !u!4 &4
Transform:
  m_GameObject: {fileID: 3}
  m_Father: {fileID: 0}
--- !u!137 &5
SkinnedMeshRenderer:
  m_GameObject: {fileID: 3}
  m_Mesh: {fileID: 4300000, guid: abcdef123456, type: 2}
  m_Bones:
  - {fileID: 2}
  m_Materials: []
"""
    (root / "Base.prefab").write_text(prefab)
    return mesh


class BridgeTests(unittest.TestCase):
    def test_common_humanoid_profiles_match_the_same_body_joints(self):
        for source,expected in [("mixamorig:LeftForeArm","Left elbow"),("upper_arm.L","Left arm"),
                                ("RightUpperLeg","Right leg"),("LeftIndexProximal","IndexFinger1_L"),
                                ("Eye_R","Eye_R"),("Chest","Chest")]:
            self.assertEqual(bridge.humanoid_name(source),expected)
        self.assertIsNone(bridge.humanoid_name("Jacket_attachment"))

    def test_folder_scan_finds_models_in_separate_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for n in ["one","two"]:
                with zipfile.ZipFile(root/(n+".zip"),"w") as z:z.writestr(n+"/Body.glb",b"fixture")
            result=bridge.scan(root)
            self.assertEqual(len(result["candidates"]),2)
            self.assertEqual({Path(c["source"]).name for c in result["candidates"]},{"one.zip","two.zip"})

    def test_unity_base_export_preserves_skin_and_mirrors_winding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tiny_base(root)
            info = bridge.unity_to_glb(bridge.Source(root), "Body.asset", root / "base.glb")
            self.assertEqual((info["vertices"], info["triangles"], info["bones"]), (3, 1, 1))
            doc = bridge.glb_doc(root / "base.glb")
            binary = (root / "base.glb").read_bytes()
            binary = binary[28 + struct.unpack_from("<I", binary, 12)[0]:]
            p = doc["meshes"][0]["primitives"][0]
            positions = bridge.accessor_array(doc, binary, p["attributes"]["POSITION"])
            self.assertEqual(positions[1, 0], -1)
            self.assertEqual(bridge.accessor_array(doc, binary, p["indices"]).reshape(-1).tolist(), [0, 2, 1])
            weights = bridge.accessor_array(doc, binary, p["attributes"]["WEIGHTS_0"])
            np.testing.assert_allclose(weights.sum(axis=1), 1)
            self.assertEqual(doc["nodes"][doc["skins"][0]["joints"][0]]["name"], "Hips")

    def test_invalid_skin_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mesh = tiny_base(root)
            mesh["m_BindPose"] = []
            with patch.object(bridge, "read_mesh", return_value=mesh):
                with self.assertRaisesRegex(ValueError, "non coincidono"):
                    bridge.unity_to_glb(bridge.Source(root), "Body.asset", root / "bad.glb")

    def test_archive_paths_and_executable_are_rejected(self):
        for entry in ["../outside", "/absolute", "C:/absolute", "a/../../b", "a\nother"]:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                bridge.clean_entry(entry)
        with tempfile.TemporaryDirectory() as temp:
            exe = Path(temp) / "avatar.exe"
            exe.write_bytes(b"MZtest")
            with self.assertRaisesRegex(ValueError, "EXE"):
                bridge.Source(exe)

    @unittest.skipUnless(Path("C:/Program Files/7-Zip/7z.exe").exists(), "7-Zip not installed")
    def test_zip_crlf_listing_and_selective_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with zipfile.ZipFile(root / "avatar.zip", "w") as z:
                z.writestr("Assets/Body.asset", b"base only")
                z.writestr("Assets/Body.asset.meta", b"guid: body\n")
                z.writestr("Assets/addon.cs", b"never executed")
                z.writestr("Library/cache.asset", b"ignore")
                z.writestr("../outside.asset", b"reject")
            source = bridge.Source(root / "avatar.zip")
            self.assertEqual(source.read("Assets/Body.asset"), b"base only")
            source.prefetch(["Assets/Body.asset.meta"])
            self.assertEqual(source.read("Assets/Body.asset.meta"), b"guid: body\n")
            self.assertNotIn("../outside.asset", source.entries)
            self.assertEqual([c["entry"] for c in bridge.scan(root / "avatar.zip")["candidates"]], ["Assets/Body.asset"])

    def test_later_rar_volume_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "avatar.part2.rar"
            path.touch()
            with self.assertRaisesRegex(ValueError, "part1"):
                bridge.Source(path)

    def test_unknown_rig_is_not_silently_destroyed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tiny_base(root)
            bridge.unity_to_glb(bridge.Source(root), "Body.asset", root / "base.glb")
            with self.assertRaisesRegex(ValueError, "non riconosciuto"):
                bridge.reduce_rig(root / "base.glb", root / "light.glb")

    def test_cyclic_glb_is_rejected_before_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cycle.glb"
            glb = bridge.GLB()
            glb.doc["nodes"] = [{"children": [1]}, {"children": [0]}]
            glb.save(path)
            with self.assertRaisesRegex(ValueError, "ciclica"):
                bridge.glb_doc(path)

    def test_external_glb_resources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "external.glb"
            glb = bridge.GLB()
            glb.doc["images"] = [{"uri": "https://example.invalid/avatar.png"}]
            glb.save(path)
            with self.assertRaisesRegex(ValueError, "riferimenti esterni"):
                bridge.glb_doc(path)

    def test_prepare_report_never_claims_playable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tiny_base(root)
            with patch.object(bridge, "DATA", root / "data"):
                result = bridge.prepare(root, "Body.asset", "male", lightweight=False)
            self.assertFalse(result["playable"])
            self.assertEqual(result["gender"], "male")
            self.assertTrue(Path(result["output"]).is_file())
            self.assertTrue((Path(result["job"]) / "report.json").is_file())


if __name__ == "__main__":
    unittest.main()
