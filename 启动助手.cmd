@echo off
setlocal
set "HDU_START_SCRIPT=%~f0"
powershell.exe -NoProfile -Command "$out=Join-Path (Split-Path -LiteralPath $env:HDU_START_SCRIPT) 'desktop\out'; $roots=@($out)+@(Get-ChildItem -LiteralPath (Join-Path $out 'releases') -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName); $apps=foreach($root in $roots) { Get-ChildItem -LiteralPath $root -Directory -Filter '*-win32-x64' -ErrorAction SilentlyContinue | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter '*.exe' } }; $exe=$apps | Sort-Object { [version]$_.VersionInfo.ProductVersion } -Descending | Select-Object -First 1; if (-not $exe) { exit 1 }; Start-Process -FilePath $exe.FullName"
if not errorlevel 1 exit /b 0
echo Open the desktop EXE from the extracted Windows x64 ZIP.
pause
exit /b 1
