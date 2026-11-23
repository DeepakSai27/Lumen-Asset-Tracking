<#
  Build / flash / monitor the checkpoint firmware.

  ESP-IDF cannot build from a path containing spaces or brackets (it resolves
  drive mappings and junctions back to the real path), so this script mirrors
  the sources to C:\esp-build\Lumen-Checkpoint, runs idf.py there, and copies
  sdkconfig back so menuconfig changes are kept in this folder.

  Examples:
    .\build.ps1                      # build
    .\build.ps1 menuconfig           # set Wi-Fi, server URL, token ("Lumen Checkpoint" menu)
    .\build.ps1 -p COM7 flash monitor
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$IdfArgs)

$src  = $PSScriptRoot
$work = "C:\esp-build\Lumen-Checkpoint"

if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
    . "C:\Espressif\tools\Microsoft.v5.5.2.PowerShell_profile.ps1" *> $null
}

# Remember the project's sdkconfig so an edit made while building is not overwritten.
$srcCfg = Join-Path $src "sdkconfig"
$cfgHashBefore = if (Test-Path $srcCfg) { (Get-FileHash $srcCfg).Hash } else { "" }

# Mirror sources (keep the work copy's build folder for fast incremental builds).
New-Item -ItemType Directory -Force $work | Out-Null
robocopy $src $work /MIR /XD build .git /XF build.ps1 /NFL /NDL /NJH /NJS /NP | Out-Null

if (-not $IdfArgs) { $IdfArgs = @("build") }
Push-Location -LiteralPath $work
try {
    if (-not (Test-Path "build")) { idf.py set-target esp32c5 }
    idf.py @IdfArgs
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

# Keep configuration with the project.
# Copy menuconfig/set-target changes back, unless sdkconfig here was edited meanwhile.
$cfgHashNow = if (Test-Path $srcCfg) { (Get-FileHash $srcCfg).Hash } else { "" }
if ((Test-Path "$work\sdkconfig") -and $cfgHashNow -eq $cfgHashBefore) {
    Copy-Item "$work\sdkconfig" -Destination $srcCfg -Force
} elseif ($cfgHashNow -ne $cfgHashBefore) {
    Write-Warning "sdkconfig changed during the build; kept your edit. Run the build again to include it."
}
if ($code -eq 0 -and (Test-Path "$work\build\Lumen-Checkpoint.bin")) {
    Write-Host "Firmware: $work\build\Lumen-Checkpoint.bin"
}
exit $code
