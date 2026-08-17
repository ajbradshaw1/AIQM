[CmdletBinding()]
param(
    [string]$PythonPath,
    [switch]$DryRun,
    [switch]$ProbeCandidates,
    [string]$ManagedEnvironmentPath = (
        "D:\Environment_Cache\conda_envs\ai4mbe-gui\python.exe"
    )
)

$ErrorActionPreference = "Stop"
$shared = Join-Path $PSScriptRoot "start_chmbe.ps1"
if (-not (Test-Path -LiteralPath $shared -PathType Leaf)) {
    throw "Shared Growth Monitor launcher is missing: $shared"
}

$parameters = @{ Chamber = "ombe" }
if ($PythonPath) {
    $parameters.PythonPath = $PythonPath
}
if ($DryRun) {
    $parameters.DryRun = $true
}
if ($ProbeCandidates) {
    $parameters.ProbeCandidates = $true
}
if ($ManagedEnvironmentPath) {
    $parameters.ManagedEnvironmentPath = $ManagedEnvironmentPath
}

& $shared @parameters
exit $LASTEXITCODE
