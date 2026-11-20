# Nemesis updater: backs up the current files, copies the newest downloaded versions, tests them,
# and puts the old files back by itself if the test fails.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$files = "nemesis.py","core.py","selftest.py","guard.py","hud.html","hud_server.py","mind.py","offline.py","phone.html","qrmini.py","tools.py","vision.py","home.py","light_test.py","nemesis_light.ino","requirements.txt","README.md"
$dirs = @("$env:USERPROFILE\Downloads","E:\download") | Where-Object { Test-Path $_ }
$pool = foreach ($d in $dirs) { Get-ChildItem -Path $d -Recurse -Depth 2 -File -ErrorAction SilentlyContinue }
function Newest($name) {
    $base = [IO.Path]::GetFileNameWithoutExtension($name); $ext = [IO.Path]::GetExtension($name)
    $rx = "^" + [regex]::Escape($base) + "( \(\d+\))?" + [regex]::Escape($ext) + "$"
    $pool | Where-Object { $_.Name -match $rx } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bk = Join-Path $here "backups\$stamp"
New-Item -ItemType Directory -Force -Path $bk | Out-Null
$changed = @()
foreach ($f in $files) {
    $new = Newest $f
    if (-not $new) { continue }
    $cur = Join-Path $here $f
    if (Test-Path $cur) {
        if ((Get-FileHash $cur -Algorithm SHA256).Hash -eq (Get-FileHash $new.FullName -Algorithm SHA256).Hash) { continue }
        Copy-Item $cur (Join-Path $bk $f) -Force
    } else { Set-Content -Path (Join-Path $bk ($f + ".new")) -Value "added" }
    Copy-Item $new.FullName $cur -Force
    $changed += $f
    $h = (Get-FileHash $cur -Algorithm SHA256).Hash.Substring(0,12).ToLower()
    Write-Host ("  updated {0,-20} {1}  (from {2})" -f $f, $h, $new.DirectoryName)
}
if ($changed.Count -eq 0) { Write-Host "Nothing new found in the download folders. Nothing changed."; Remove-Item $bk -Recurse -Force; exit 0 }
Write-Host "`nTesting the new files (this touches nothing on your PC)..."
if (Test-Path "venv\Scripts\Activate.ps1") { . "venv\Scripts\Activate.ps1" }
$env:PYTHONUTF8 = "1"
& python nemesis.py --selftest
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nThe test FAILED - putting your old files back." -ForegroundColor Red
    foreach ($f in $changed) {
        $b = Join-Path $bk $f
        if (Test-Path $b) { Copy-Item $b (Join-Path $here $f) -Force } else { Remove-Item (Join-Path $here $f) -Force -ErrorAction SilentlyContinue }
    }
    Write-Host "Old version restored. Nothing was lost." -ForegroundColor Yellow
    exit 1
}
& python nemesis.py --seal
Write-Host "`nUpdate finished and sealed. Backup of the old files: $bk" -ForegroundColor Green
Write-Host "Start Nemesis as usual. To go back:  powershell -ExecutionPolicy Bypass -File .\rollback_nemesis.ps1"
