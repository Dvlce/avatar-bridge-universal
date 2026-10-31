import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import bridge
import compat25
import cp77


class Compat25Tests(unittest.TestCase):
    def test_merge_keeps_player_gameplay_and_resource_references(self):
        skin = {"$type": "entMorphTargetSkinnedMeshComponent", "id": "2", "name": cp77.name("body"),
                "morphResource": cp77.ref("base\\original.morphtarget"),
                "binding": {"HandleRefId": "30001"}}
        native = {"entity": {"Data": {"$type": "PlayerPuppet", "persistentData": "keep"}},
                  "components": [{"id": "1", "name": cp77.name("camera")}, copy.deepcopy(skin)],
                  "compiledData": {"Data": {"Chunks": [{"id": "1", "camera": True}, copy.deepcopy(skin)],
                                             "CruidDict": {"0": "1", "1": "2"}}}}
        addition = cp77.component("body", 12)
        compat25.apply_components(native, [cp77.disabled_component(skin), addition])
        self.assertEqual(native["entity"]["Data"]["$type"], "PlayerPuppet")
        self.assertTrue(native["compiledData"]["Data"]["Chunks"][0]["camera"])
        for group in (native["components"], native["compiledData"]["Data"]["Chunks"]):
            original = next(comp for comp in group if comp["id"] == "2")
            self.assertEqual(original["morphResource"], skin["morphResource"])
            self.assertEqual(original["binding"], {"HandleRefId": "30001"})
            self.assertEqual(original["chunkMask"], "0")
        added = native["compiledData"]["Data"]["Chunks"][-1]
        root = native["components"][-1]
        self.assertGreater(int(added["skinning"]["HandleId"]), 30001)
        self.assertEqual(root["skinning"], {"HandleRefId": added["skinning"]["HandleId"]})
        self.assertEqual(native["compiledData"]["Data"]["CruidDict"]["2"], addition["id"])

    def test_uncooked_resource_is_rejected_without_modifying_it(self):
        native = {"components": []}
        before = copy.deepcopy(native)
        with self.assertRaisesRegex(ValueError, "compilati"):
            compat25.apply_components(native, [cp77.component("body", 1)])
        self.assertEqual(native, before)

    def test_resource_cannot_escape_archive(self):
        for value in [r"..\outside.mesh", r"C:\outside.mesh", r"\\server\file.mesh"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                compat25.checked_relative(value)

    def test_equal_native_filenames_keep_separate_cache_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ["cheeks", "freckles"]:
                target = root / "cache/distinct" / directory / "same.app.json"
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps({"origin": directory}))
            for directory in ["cheeks", "freckles"]:
                document = compat25.native_document(root / "originals", root / "cache",
                                                    Path(directory) / "same.app", {"same.app"})
                self.assertEqual(document["origin"], directory)

    def test_only_the_target_game_process_blocks_installation(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "game"
            test_exe = root / "test/bin/x64/Cyberpunk2077.exe"
            for path, blocked in [(test_exe, False), (target / "bin/x64/Cyberpunk2077.exe", True)]:
                output = json.dumps([str(path)]).encode()
                with self.subTest(path=path), patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                    self.assertEqual(cp77.running_game(target), blocked)

    def test_compat_install_and_remove_restore_native_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, data = root / "game", root / "data"
            for relative in ["bin/x64/Cyberpunk2077.exe", "bin/x64/version.dll", "bin/x64/plugins/cyber_engine_tweaks.asi"]:
                path = game / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"fixture")
            job = data / "jobs/avatar_test"
            package = job / "package"; package.mkdir(parents=True)
            for filename in ["zz_avatarbridge.archive", "zz_avatarbridge.archive.xl", "init.lua", "AvatarBridge25.asi"]:
                (package / filename).write_bytes(filename.encode())
            info = {"package": str(package), "sha256": compat25.digest(package / "zz_avatarbridge.archive")}
            (job / "build.json").write_text(json.dumps(info))
            destination = game / "bin/x64/plugins/AvatarBridge25.asi"
            destination.write_bytes(b"previous native component")
            scripts = game / "engine/config/base/scripts.ini"
            scripts.parent.mkdir(parents=True)
            scripts.write_bytes(b"[Scripts]\nEnableCompilation = true")
            def versions(path):
                return {"ProductVersion": "2.5"} if path.name == "Cyberpunk2077.exe" else {}
            with patch.object(bridge, "DATA", data), patch.object(bridge, "windows_version_info", side_effect=versions), \
                    patch.object(compat25, "supported", return_value=True), patch.object(compat25, "RUNTIME", package / "AvatarBridge25.asi"), \
                    patch.object(compat25, "build", return_value=info), patch.object(cp77, "running_game", return_value=False):
                status = bridge.runtime_status(game)
                self.assertFalse(status["ready"])
                self.assertTrue(status["can_install"])
                result = cp77.install(job, game)
                self.assertEqual(result["mode"], "compat25")
                self.assertEqual(destination.read_bytes(), b"AvatarBridge25.asi")
                self.assertFalse(scripts.exists())
                cp77.uninstall(game)
            self.assertEqual(destination.read_bytes(), b"previous native component")
            self.assertEqual(scripts.read_bytes(), b"[Scripts]\nEnableCompilation = true")
            self.assertFalse((game / "archive/pc/mod/zz_avatarbridge.archive").exists())

    def test_active_old_loader_cannot_enter_compat_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary)
            for relative in ["bin/x64/Cyberpunk2077.exe", "bin/x64/version.dll", "bin/x64/winmm.dll",
                             "bin/x64/plugins/cyber_engine_tweaks.asi"]:
                path = game / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"fixture")
            with patch.object(bridge, "windows_version_info", return_value={"ProductVersion": "2.5"}), \
                    patch.object(compat25, "supported", return_value=True), patch.object(compat25, "RUNTIME", game / "bin/x64/version.dll"):
                self.assertFalse(bridge.runtime_status(game)["can_install"])


if __name__ == "__main__":
    unittest.main()
