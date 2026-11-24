param([string]$Python = '', [switch]$BundleOnly)
$ErrorActionPreference = 'Stop'
$desktop = $PSScriptRoot
$root = Split-Path -Parent $desktop
$bundle = Join-Path $desktop '.bundle'
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Use -Python to specify a 64-bit Python executable with PyInstaller and Pillow installed.'
}

Push-Location $root
try {
    New-Item -ItemType Directory -Path $bundle -Force | Out-Null
    & $Python (Join-Path $desktop 'build_icon.py') (Join-Path $bundle 'app.ico')
    if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
    & $Python -m PyInstaller --noconfirm --onedir --console --name venue-engine --paths $root --distpath $bundle --workpath (Join-Path $bundle 'work') --specpath $bundle (Join-Path $root 'venue_desktop.py')
    if ($LASTEXITCODE -ne 0) { throw 'Backend packaging failed.' }
} finally {
    Pop-Location
}
if ($BundleOnly) { return }

Push-Location $desktop
try {
    & npm.cmd run dist
    if ($LASTEXITCODE -ne 0) { throw 'Electron packaging failed.' }
} finally {
    Pop-Location
}
