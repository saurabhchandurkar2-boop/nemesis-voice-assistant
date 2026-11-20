# Puts back the files saved by the last update (or the one named, e.g. 20261103_164500).
param([string]$Stamp = "")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$root = Join-Path $here "backups"
if (-not (Test-Path $root)) { Write-Host "No backups found."; exit 1 }
$all = Get-ChildItem $root -Directory | Sort-Object Name -Descending
if ($Stamp) { $pick = $all | Where-Object Name -eq $Stamp | Select-Object -First 1 } else { $pick = $all | Select-Object -First 1 }
if (-not $pick) { Write-Host "Backup not found. Available:"; $all | ForEach-Object { "  " + $_.Name }; exit 1 }
foreach ($f in Get-ChildItem $pick.FullName -File) {
    if ($f.Name.EndsWith(".new")) { Remove-Item (Join-Path $here $f.Name.Substring(0, $f.Name.Length - 4)) -Force -ErrorAction SilentlyContinue; continue }
    Copy-Item $f.FullName (Join-Path $here $f.Name) -Force
    Write-Host "  restored $($f.Name)"
}
if (Test-Path "venv\Scripts\Activate.ps1") { . "venv\Scripts\Activate.ps1" }
& python nemesis.py --seal
Write-Host "`nRolled back to $($pick.Name)." -ForegroundColor Green
