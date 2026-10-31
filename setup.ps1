param([switch]$WithWolvenKit,[switch]$WithBlender)
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$taskData = Join-Path $env:LOCALAPPDATA 'CyberpunkAvatarBridge'
$runtime = Join-Path $taskData 'runtime'
New-Item -ItemType Directory -Path $taskData -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'Scripts\python.exe'))) {
    & py -3.12 -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Installa Python 3.12 e riprova.' }
}
& (Join-Path $runtime 'Scripts\python.exe') -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze non riuscita.' }
$toolsRoot = Join-Path $taskData 'tools'
New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
$optimizerZip = Join-Path $toolsRoot 'gltfpack-windows-v1.3.zip'
if (-not (Test-Path -LiteralPath (Join-Path $toolsRoot 'gltfpack\gltfpack.exe'))) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/zeux/meshoptimizer/releases/download/v1.3/gltfpack-windows.zip' -OutFile $optimizerZip
    Expand-Archive -LiteralPath $optimizerZip -DestinationPath (Join-Path $toolsRoot 'gltfpack') -Force
}
if ($WithBlender -and -not (Test-Path -LiteralPath (Join-Path $toolsRoot 'blender\blender-4.5.9-windows-x64\blender.exe'))) {
    $blenderZip=Join-Path $toolsRoot 'blender-4.5.9-windows-x64.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://download.blender.org/release/Blender4.5/blender-4.5.9-windows-x64.zip' -OutFile $blenderZip
    Expand-Archive -LiteralPath $blenderZip -DestinationPath (Join-Path $toolsRoot 'blender') -Force
}
if ($WithWolvenKit) {
    $toolsRoot = Join-Path $taskData 'tools'
    New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
    $download = Join-Path $toolsRoot 'WolvenKit.Console-9.1.0.zip'
    if (-not (Test-Path -LiteralPath $download)) {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/WolvenKit/WolvenKit/releases/download/9.1.0/WolvenKit.Console-9.1.0.zip' -OutFile $download
    }
    Expand-Archive -LiteralPath $download -DestinationPath (Join-Path $toolsRoot 'WolvenKit') -Force
}
& dotnet publish (Join-Path $PSScriptRoot 'src\AvatarBridge.csproj') -c Release -r win-x64 --self-contained false '-p:PublishSingleFile=true' -o (Join-Path $PSScriptRoot 'App') --nologo
if ($LASTEXITCODE -ne 0) { throw 'Compilazione non riuscita.' }
Write-Host 'Pronto: App\AvatarBridge.exe. Il setup scarica solo strumenti e dipendenze; non carica file personali.'
