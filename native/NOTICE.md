# AvatarBridge25 runtime

This is a small, private ASI plugin for the local Cyberpunk 2.5 executable listed
in `build-info.json`. It registers only methods named `AB*`. It does not provide
ArchiveXL, redscript, Codeware's script API, or services for other mods.

The startup/bootstrap, reflection helpers and build layout derive from
[Codeware](https://github.com/psiberx/cp2077-codeware), copyright Pavel Siberx,
under the MIT license. The [RED4ext SDK](https://github.com/WopsS/RED4ext.SDK)
is also used under its MIT license. Both licenses accompany the plugin.
Codeware's bundled Core/Support/Red helper code and xmake dependencies are used
for initialization and RTTI registration. The full Codeware services are excluded.
The generated address table resolves the SDK directly, without loading RED4ext.

`src/AvatarRuntime.cpp` adds component enumeration, mesh resource hashes,
visibility masks and asynchronous face mesh loading. Resource references and
gameplay entity data remain intact. `src/Main.cpp` checks the executable's PE
timestamp and image size before enabling the native component. The Python
installer additionally requires the complete executable SHA256 to match.

Appearance updates wait for an attached entity, a loaded mesh and, for skinned
and morph components, a render proxy. Facial replacement waits for the preceding
asynchronous mesh load before submitting another one. These checks also apply
when the asynchronous refresh callback runs, after a menu or scene may have closed.

To rebuild on Windows, install Git, xmake and Visual Studio C++ with a Windows
SDK, then run `python native/build-runtime.py`. It creates a dedicated checkout
under `native/.build`, checks out the exact revisions in `build-info.json`,
overlays these source files and builds the ASI. An existing game installation is
never modified by that script. The shipped binary was compiled and tested locally;
the portable SDK build used in this repair is recorded in `build-info.json`.

The Python and Lua compatibility builder lives in `engine/compat25.py` and
`engine/compat25.lua`. Generated archives contain native game resources extracted
locally and must not be redistributed with the application's source package.
