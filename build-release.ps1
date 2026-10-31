param([string]$Repository = $env:GITHUB_REPOSITORY)
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
if ($Repository -notmatch '^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$') { throw 'Repository richiesto: proprietario/nome' }
$releaseRoot = Join-Path $PSScriptRoot 'dist'
$releaseApp = Join-Path $releaseRoot 'AvatarBridge'
$releaseDownloads = Join-Path $releaseRoot 'downloads'
New-Item -ItemType Directory -Path $releaseApp,$releaseDownloads -Force | Out-Null
& dotnet publish (Join-Path $PSScriptRoot 'src\AvatarBridge.csproj') -c Release -r win-x64 --self-contained true '-p:PublishSingleFile=true' '-p:IncludeNativeLibrariesForSelfExtract=true' -o $releaseApp --nologo
if ($LASTEXITCODE -ne 0) { throw 'Compilazione release fallita' }
$embeddedArchive = Join-Path $releaseDownloads 'python-3.12.10-embed-amd64.zip'
if (-not (Test-Path -LiteralPath $embeddedArchive)) { Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile $embeddedArchive }
$embeddedRuntime = Join-Path $releaseApp 'runtime'
Expand-Archive -LiteralPath $embeddedArchive -DestinationPath $embeddedRuntime -Force
@('python312.zip','.','Lib/site-packages','../engine','import site') | Set-Content -LiteralPath (Join-Path $embeddedRuntime 'python312._pth') -Encoding ASCII
& python -m pip install --only-binary=:all: --target (Join-Path $embeddedRuntime 'Lib\site-packages') --upgrade -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Preparazione runtime Python fallita' }
$optimizerArchive = Join-Path $releaseDownloads 'gltfpack.zip'
if (-not (Test-Path -LiteralPath $optimizerArchive)) { Invoke-WebRequest -UseBasicParsing 'https://github.com/zeux/meshoptimizer/releases/download/v1.3/gltfpack-windows.zip' -OutFile $optimizerArchive }
Expand-Archive -LiteralPath $optimizerArchive -DestinationPath (Join-Path $releaseApp 'tools\gltfpack') -Force
Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/zeux/meshoptimizer/v1.3/LICENSE.md' -OutFile (Join-Path $releaseApp 'tools\gltfpack\LICENSE.md')
$sevenZipFolder = Join-Path $env:ProgramFiles '7-Zip'
if (-not (Test-Path -LiteralPath (Join-Path $sevenZipFolder '7z.exe'))) { throw 'Installa 7-Zip per creare la distribuzione completa.' }
$bundledSevenZip = Join-Path $releaseApp 'tools\7zip'
New-Item -ItemType Directory -Path $bundledSevenZip -Force | Out-Null
foreach ($sevenFile in @('7z.exe','7z.dll','License.txt','copying.txt')) {
    if (Test-Path -LiteralPath (Join-Path $sevenZipFolder $sevenFile)) { Copy-Item -LiteralPath (Join-Path $sevenZipFolder $sevenFile) -Destination $bundledSevenZip -Force }
}
@{Repository=$Repository;Automatic=$true} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseApp 'distribution.json') -Encoding ASCII
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE'),(Join-Path $PSScriptRoot 'README.md') -Destination $releaseApp -Force
$releaseLicenses = Join-Path $releaseApp 'licenses'
New-Item -ItemType Directory -Path $releaseLicenses -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'native\NOTICE.md'),(Join-Path $PSScriptRoot 'native\THIRD-PARTY-LICENSES.txt'),(Join-Path $PSScriptRoot 'native\LICENSE-Codeware.txt'),(Join-Path $PSScriptRoot 'native\LICENSE-RED4ext.SDK.txt') -Destination $releaseLicenses -Force
& (Join-Path $embeddedRuntime 'python.exe') -c 'import numpy, scipy, yaml, PIL; import sys; print(sys.version)'
if ($LASTEXITCODE -ne 0) { throw 'Runtime portabile non funzionante' }
$releaseZip = Join-Path $releaseRoot 'AvatarBridge-win-x64.zip'
Compress-Archive -Path (Join-Path $releaseApp '*') -DestinationPath $releaseZip -Force -CompressionLevel Optimal
$releaseHash = (Get-FileHash -LiteralPath $releaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
"$releaseHash  AvatarBridge-win-x64.zip" | Set-Content -LiteralPath ($releaseZip + '.sha256') -Encoding ASCII
Write-Host "Release: $releaseZip"
