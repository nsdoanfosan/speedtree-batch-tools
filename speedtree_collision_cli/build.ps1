[CmdletBinding()]
param(
    [ValidateSet("Release", "Debug")]
    [string]$Configuration = "Release",

    [switch]$IfNeeded
)

$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $PSCommandPath
$outputDirectory = Join-Path $sourceDirectory "bin"
$vcvars = "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"

if (-not (Test-Path -LiteralPath $vcvars -PathType Leaf)) {
    throw "Visual Studio 2022 vcvars64.bat was not found: $vcvars"
}

New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

$common = if ($Configuration -eq "Release") {
    "/nologo /std:c++20 /O2 /EHsc /W4 /DUNICODE /D_UNICODE"
} else {
    "/nologo /std:c++20 /Od /Zi /EHsc /W4 /DUNICODE /D_UNICODE"
}

$hookSource = Join-Path $sourceDirectory "hook.cpp"
$launcherSource = Join-Path $sourceDirectory "launcher.cpp"
$hookOutput = Join-Path $outputDirectory "speedtree_collision_hook.dll"
$launcherOutput = Join-Path $outputDirectory "speedtree_collision_cli.exe"
$hookObject = Join-Path $outputDirectory "hook.obj"
$launcherObject = Join-Path $outputDirectory "launcher.obj"
$capabilityContract = "SPEEDTREE_COLLISION_CLI_CONTRACT=native-runtime-receipt-v16"

if ($IfNeeded -and
    (Test-Path -LiteralPath $hookOutput -PathType Leaf) -and
    (Test-Path -LiteralPath $launcherOutput -PathType Leaf)) {
    $inputPaths = @(
        $PSCommandPath,
        $hookSource,
        $launcherSource,
        (Join-Path $sourceDirectory "session_protocol.h")
    )
    $latestInput = ($inputPaths | ForEach-Object {
        (Get-Item -LiteralPath $_).LastWriteTimeUtc
    } | Measure-Object -Maximum).Maximum
    $oldestOutput = (@($hookOutput, $launcherOutput) | ForEach-Object {
        (Get-Item -LiteralPath $_).LastWriteTimeUtc
    } | Measure-Object -Minimum).Minimum

    if ($oldestOutput -ge $latestInput) {
        $diagnoseOutput = @(& $launcherOutput --diagnose 2>$null)
        if ($LASTEXITCODE -eq 0 -and $diagnoseOutput -contains $capabilityContract) {
            Write-Host "SpeedTree collision CLI is up to date."
            return
        }
        Write-Host "SpeedTree collision CLI capability contract is stale; rebuilding."
    }
}

$hookCommand = 'call "{0}" >nul && cl.exe {1} /Fo"{2}" /LD "{3}" /link user32.lib gdi32.lib opengl32.lib /OUT:"{4}"' -f `
    $vcvars, $common, $hookObject, $hookSource, $hookOutput
$launcherCommand = 'call "{0}" >nul && cl.exe {1} /Fo"{2}" "{3}" /link bcrypt.lib /OUT:"{4}"' -f `
    $vcvars, $common, $launcherObject, $launcherSource, $launcherOutput

& cmd.exe /d /s /c $hookCommand
if ($LASTEXITCODE -ne 0) {
    throw "Hook DLL compilation failed with exit code $LASTEXITCODE"
}

& cmd.exe /d /s /c $launcherCommand
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $launcherOutput"
Write-Host "Built: $hookOutput"
