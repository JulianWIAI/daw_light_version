# build_win.ps1 -- Build daw_processors from a short path to avoid MAX_PATH.
# Run from any directory: powershell -ExecutionPolicy Bypass -File build_win.ps1

$ProjectCpp = Split-Path $MyInvocation.MyCommand.Path -Parent
$BuildDir   = "C:\tmp\dawbuild\cpp_processors"

Write-Host "-- Copying sources to $BuildDir"
if (Test-Path $BuildDir\build) { Remove-Item $BuildDir\build -Recurse -Force }
if (Test-Path $BuildDir)       { Remove-Item $BuildDir -Recurse -Force }
Copy-Item $ProjectCpp $BuildDir -Recurse -Force

Write-Host "-- Building extension"
Push-Location $BuildDir
python setup.py build_ext --inplace
$exitCode = $LASTEXITCODE
Pop-Location

if ($exitCode -ne 0) {
    Write-Host "BUILD FAILED (exit $exitCode)" -ForegroundColor Red
    exit $exitCode
}

Write-Host "-- Copying .pyd back to project"
$pyd = Get-ChildItem $BuildDir -Filter "*.pyd" | Select-Object -First 1
if ($pyd) {
    Copy-Item $pyd.FullName "$ProjectCpp\$($pyd.Name)" -Force
    Write-Host "Installed: $($pyd.Name)" -ForegroundColor Green
} else {
    Write-Host "No .pyd found after build!" -ForegroundColor Red
    exit 1
}
