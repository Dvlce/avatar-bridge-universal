"""Rebuild the bundled ASI using a dedicated, pinned Codeware checkout."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", default="git")
    parser.add_argument("--xmake", default="xmake")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    info = json.loads((root / "build-info.json").read_text())
    checkout = root / ".build/Codeware"
    def run(*command, cwd=None):
        subprocess.run(list(map(str, command)), cwd=cwd, check=True)
    if not checkout.exists():
        run(args.git, "clone", "https://github.com/psiberx/cp2077-codeware.git", checkout)
        run(args.git, "checkout", info["codeware"], cwd=checkout)
        run(args.git, "submodule", "update", "--init", "--recursive", cwd=checkout)
        run(args.git, "checkout", info["red4ext_sdk"], cwd=checkout / "vendor/RED4ext.SDK")
    else:
        # Preserve local work: an unexpected checkout is never reset or cleaned.
        for directory, expected in [(checkout, info["codeware"]),
                                     (checkout / "vendor/RED4ext.SDK", info["red4ext_sdk"])]:
            actual = subprocess.check_output([args.git, "rev-parse", "HEAD"], cwd=directory, text=True).strip()
            if actual != expected:
                raise SystemExit(f"Unexpected checkout revision: {directory}")
    for source in list((root / "src").rglob("*")) + [root / "xmake.lua"]:
        if source.is_file():
            target = checkout / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    run(args.xmake, "f", "-c", "-m", "release", "--yes", cwd=checkout)
    run(args.xmake, "build", "--yes", "-j", "4", cwd=checkout)
    destination = root.parent / "engine/runtime/AvatarBridge25.asi"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(checkout / "build/windows/x64/release/AvatarBridge25.asi", destination)
    print(destination)


if __name__ == "__main__":
    main()
