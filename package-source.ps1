$ErrorActionPreference = 'Stop'
$codeFiles = @('.gitignore','LICENSE','README.md','requirements.txt','setup.ps1','package-source.ps1',
 'src\AvatarBridge.csproj','src\app.manifest','src\App.xaml','src\App.xaml.cs',
 'src\MainWindow.xaml','src\MainWindow.xaml.cs','src\ModelReader.cs','engine\bridge.py','engine\cp77.py','engine\library.py','engine\blender_base.py','engine\geometry.py','engine\physics.py','engine\garments.py','engine\facial.py','engine\compat25.py','engine\compat25.lua','engine\runtime\AvatarBridge25.asi','native\NOTICE.md','native\THIRD-PARTY-LICENSES.txt','native\build-info.json','native\build-runtime.py','native\LICENSE-Codeware.txt','native\LICENSE-RED4ext.SDK.txt','native\src\Main.cpp','native\src\App\Application.cpp','native\src\pch.hpp','native\src\AvatarRuntime.cpp','native\src\AvatarAddresses.cpp','native\xmake.lua','tests\test_body_profile.py','tests\test_compat25.py','tests\test_bridge.py','tests\test_cp77.py')
$codeFiles += @('launcher\CyberpunkLauncher.csproj','launcher\App.xaml','launcher\App.xaml.cs','launcher\MainWindow.xaml','launcher\MainWindow.xaml.cs')
$codeFiles += @('engine\gmod.py','engine\targets.py','src\Universal.cs','src\Updater.cs','tests\test_gmod.py','build-release.ps1','.github\workflows\release.yml')
$sourceZip = Join-Path $PSScriptRoot 'AvatarBridge-source.zip'
Add-Type -AssemblyName System.IO.Compression
$zipStream = [System.IO.File]::Open($sourceZip, [System.IO.FileMode]::Create)
try {
    $zip = [System.IO.Compression.ZipArchive]::new($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($codeFile in $codeFiles) {
            $sourceFile = Join-Path $PSScriptRoot $codeFile
            if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "Sorgente mancante: $codeFile" }
            $entry = $zip.CreateEntry($codeFile.Replace('\','/'))
            $entryStream = $entry.Open()
            try {
                $content = [System.IO.File]::ReadAllBytes($sourceFile)
                $entryStream.Write($content,0,$content.Length)
            } finally { $entryStream.Dispose() }
        }
    } finally { $zip.Dispose() }
} finally { $zipStream.Dispose() }
Write-Host "Archivio dei soli sorgenti: $sourceZip"
