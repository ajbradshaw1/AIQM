[CmdletBinding()]
param(
    [ValidateSet("chmbe", "ombe")]
    [string]$Chamber = "chmbe",

    [string]$PythonPath,

    # Resolve and describe the launch without opening Qt or hardware.
    [switch]$DryRun,

    # Dry-run dependency probing is opt-in so offline tests do not import Qt.
    [switch]$ProbeCandidates,

    [string]$ManagedEnvironmentPath = (
        "D:\Environment_Cache\conda_envs\ai4mbe-gui\python.exe"
    )
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Add-PythonCandidate {
    param(
        [System.Collections.ArrayList]$Candidates,
        [string]$Path,
        [string]$Source
    )
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        [void]$Candidates.Add(
            [pscustomobject]@{ Path = $Path; Source = $Source }
        )
    }
}

function Get-RequiredProductionModules {
    param([string]$ChamberName)

    # Both production profiles start on Vimba, ADS, and a serial pyrometer.
    # O-MBE uses pymodbus; Ch-MBE's verified backend is the repository's raw
    # serial implementation and therefore needs pyserial but not pymodbus.
    $modules = @("vmbpy", "pyads", "serial")
    if ($ChamberName -eq "ombe") {
        $modules += "pymodbus"
    }
    return $modules
}

function New-DependencyStatus {
    param([string[]]$Modules, [object]$Value)

    $status = [ordered]@{}
    foreach ($moduleName in $Modules) {
        $status[$moduleName] = $Value
    }
    return [pscustomobject]$status
}

function Get-PythonCandidates {
    param([string]$RepositoryRoot)

    $candidates = New-Object System.Collections.ArrayList
    Add-PythonCandidate $candidates $PythonPath "-PythonPath"
    Add-PythonCandidate $candidates $env:AI4MBE_GUI_PYTHON `
        "AI4MBE_GUI_PYTHON"
    Add-PythonCandidate $candidates `
        (Join-Path $RepositoryRoot ".venv\Scripts\python.exe") `
        "repository .venv"
    if ($env:CONDA_PREFIX) {
        Add-PythonCandidate $candidates `
            (Join-Path $env:CONDA_PREFIX "python.exe") `
            "active CONDA_PREFIX"
    }
    if ($env:USERPROFILE) {
        $registered = Join-Path $env:USERPROFILE ".conda\environments.txt"
        if (Test-Path -LiteralPath $registered -PathType Leaf) {
            foreach ($prefix in Get-Content -LiteralPath $registered) {
                $trimmed = $prefix.Trim()
                if ($trimmed -and (
                    (Split-Path -Leaf $trimmed.TrimEnd("\", "/")) `
                        -ieq "ai4mbe-gui"
                )) {
                    Add-PythonCandidate $candidates `
                        (Join-Path $trimmed "python.exe") `
                        "registered ai4mbe-gui environment"
                }
            }
        }
    }
    Add-PythonCandidate $candidates $ManagedEnvironmentPath `
        "managed workstation fallback"
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pathPython) {
        Add-PythonCandidate $candidates $pathPython.Source "PATH"
    }
    return $candidates
}

function Invoke-NativeCapture {
    param([string]$Executable, [string[]]$Arguments)
    $output = @()
    $exitCode = -1
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $Executable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = (($output | ForEach-Object { "$_" }) -join `
            [Environment]::NewLine)
    }
}

function Resolve-Ai4mbePython {
    param(
        [string]$RepositoryRoot,
        [bool]$SkipProbe,
        [string]$ChamberName
    )

    $seen = @{}
    $failures = New-Object System.Collections.Generic.List[string]
    $requiredModules = @(Get-RequiredProductionModules $ChamberName)
    foreach ($candidate in Get-PythonCandidates $RepositoryRoot) {
        if (-not (Test-Path -LiteralPath $candidate.Path -PathType Leaf)) {
            if ($candidate.Source -in @("-PythonPath", "AI4MBE_GUI_PYTHON")) {
                [void]$failures.Add(
                    "$($candidate.Source): file not found: $($candidate.Path)"
                )
            }
            continue
        }
        $resolved = (Resolve-Path -LiteralPath $candidate.Path).Path
        $key = $resolved.ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            continue
        }
        $seen[$key] = $true
        if ($SkipProbe) {
            return [pscustomobject]@{
                Path = $resolved
                Source = $candidate.Source
                RequiredModules = $requiredModules
                DependencyProbe = "skipped_dry_run"
                DependencyStatus = (
                    New-DependencyStatus $requiredModules "not_probed"
                )
            }
        }

        # Match growth_monitor_app.py's Windows DLL rule: torch must load its
        # Intel OpenMP runtime before PyQt6 attempts plugin loading.  Import
        # every module required by this chamber's production defaults as part
        # of the same candidate check: an unrelated base/.venv must not win
        # merely because it can render Qt while lacking camera/PLC/serial I/O.
        $driverChecks = @{
            vmbpy = (
                "import vmbpy; assert hasattr(vmbpy, 'VmbSystem')"
            )
            pyads = (
                "import pyads; assert hasattr(pyads, 'Connection')"
            )
            serial = (
                "import serial; assert hasattr(serial, 'Serial')"
            )
            pymodbus = (
                "from pymodbus.client import ModbusSerialClient"
            )
        }
        $driverImports = (
            $requiredModules | ForEach-Object { $driverChecks[$_] }
        ) -join "; "
        $probeCode = (
            "import struct; assert struct.calcsize('P') == 8; " +
            "import torch; import PyQt6, numpy, PIL; " +
            $driverImports
        )
        $probe = Invoke-NativeCapture $resolved @(
            "-I",
            "-c",
            $probeCode
        )
        if ($probe.ExitCode -eq 0) {
            return [pscustomobject]@{
                Path = $resolved
                Source = $candidate.Source
                RequiredModules = $requiredModules
                DependencyProbe = "passed"
                DependencyStatus = (
                    New-DependencyStatus $requiredModules $true
                )
            }
        }
        $summary = ([string]$probe.Output).Trim()
        if ($summary.Length -gt 500) {
            $summary = $summary.Substring(0, 500) + "..."
        }
        [void]$failures.Add("$($candidate.Source): $resolved`n$summary")
    }

    $details = if ($failures.Count) {
        "`nCandidates rejected:`n" + ($failures -join "`n---`n")
    }
    else {
        ""
    }
    throw (
        "Could not find a 64-bit GUI Python with torch, PyQt6, numpy, " +
        "Pillow, and the $ChamberName production drivers " +
        "($($requiredModules -join ', ')).$details`nUse the repository " +
        ".venv, activate ai4mbe-gui, or " +
        "set AI4MBE_GUI_PYTHON to its python.exe."
    )
}

function Get-GitValue {
    param([string]$RepositoryRoot, [string[]]$Arguments)
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) {
        $git = Get-Command git -ErrorAction SilentlyContinue
    }
    if (-not $git) {
        return ""
    }
    $result = Invoke-NativeCapture $git.Source (
        @("-C", $RepositoryRoot) + $Arguments
    )
    if ($result.ExitCode -ne 0) {
        return ""
    }
    return $result.Output.Trim()
}

function Show-LaunchMessage {
    param([string]$Text, [string]$Title, [bool]$IsError)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = if ($IsError) {
            [System.Windows.Forms.MessageBoxIcon]::Error
        }
        else {
            [System.Windows.Forms.MessageBoxIcon]::Information
        }
        [void][System.Windows.Forms.MessageBox]::Show(
            $Text,
            $Title,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            $icon
        )
    }
    catch {
        Write-Host $Text
    }
}

$sanitizedVariables = @(
    "PYTHONHOME",
    "PYTHONPATH",
    "QT_QPA_PLATFORM",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "MPLBACKEND"
)

try {
    $repositoryRoot = (
        Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
    ).Path
    $entryFile = if ($Chamber -eq "ombe") {
        "growth_monitor_ombe.py"
    }
    else {
        "growth_monitor_chmbe.py"
    }
    $applicationLabel = if ($Chamber -eq "ombe") { "O-MBE" } else { "Ch-MBE" }
    $entry = Join-Path $repositoryRoot $entryFile
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "$applicationLabel entry point is missing: $entry"
    }

    foreach ($variable in $sanitizedVariables) {
        Remove-Item -LiteralPath "Env:$variable" -ErrorAction SilentlyContinue
    }
    $env:PYTHONNOUSERSITE = "1"
    $env:PIP_USER = "no"
    # Never trust a stale user/machine chamber variable.
    $env:AIQM_CHAMBER = $Chamber

    $skipProbe = [bool]$DryRun -and -not [bool]$ProbeCandidates
    $python = Resolve-Ai4mbePython $repositoryRoot $skipProbe $Chamber
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $localAppData) {
        $localAppData = $env:TEMP
    }
    $logDirectory = Join-Path $localAppData "AI4MBE\LauncherLogs"
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"

    $launchPlan = [ordered]@{
        dry_run = [bool]$DryRun
        application = $Chamber
        repository_root = $repositoryRoot
        working_directory = $repositoryRoot
        python = $python.Path
        python_source = $python.Source
        required_production_modules = @($python.RequiredModules)
        dependency_probe = $python.DependencyProbe
        dependency_status = $python.DependencyStatus
        git_branch = Get-GitValue $repositoryRoot @(
            "symbolic-ref", "--short", "-q", "HEAD"
        )
        git_commit = Get-GitValue $repositoryRoot @("rev-parse", "HEAD")
        arguments = @($entryFile)
        chamber = $env:AIQM_CHAMBER
        python_no_user_site = $env:PYTHONNOUSERSITE
        sanitized_variables = $sanitizedVariables
        log_directory = $logDirectory
    }
    if ($DryRun) {
        $launchPlan | ConvertTo-Json -Depth 5
        exit 0
    }

    $chamberProbe = Invoke-NativeCapture $python.Path @(
        "-I",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from drivers.config import get_active_config; assert get_active_config().chamber_id == sys.argv[2]",
        $repositoryRoot,
        $Chamber
    )
    if ($chamberProbe.ExitCode -ne 0) {
        throw "$applicationLabel chamber preflight failed: $($chamberProbe.Output)"
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logStem = Join-Path $logDirectory "$Chamber-$timestamp"
    $metadataLog = "$logStem-launch.json"
    $stdoutLog = "$logStem-stdout.log"
    $stderrLog = "$logStem-stderr.log"
    $launchPlan | ConvertTo-Json -Depth 5 | Set-Content `
        -LiteralPath $metadataLog -Encoding UTF8

    $process = Start-Process `
        -FilePath $python.Path `
        -ArgumentList @($entryFile) `
        -WorkingDirectory $repositoryRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        throw (
            "$applicationLabel Growth Monitor exited with code $($process.ExitCode)." +
            "`nError log: $stderrLog"
        )
    }
    exit 0
}
catch {
    $message = $_.Exception.Message
    $fallbackRoot = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $fallbackRoot) {
        $fallbackRoot = $env:TEMP
    }
    $fallbackDirectory = Join-Path $fallbackRoot "AI4MBE\LauncherLogs"
    try {
        New-Item -ItemType Directory -Path $fallbackDirectory -Force | Out-Null
        $failureLog = Join-Path $fallbackDirectory (
            "launcher-failure-" + (Get-Date -Format "yyyyMMdd_HHmmss_fff") +
            ".log"
        )
        $message | Set-Content -LiteralPath $failureLog -Encoding UTF8
        $message = "$message`n`nLauncher log: $failureLog"
    }
    catch {
        # The dialog still contains the original failure when logging fails.
    }
    $failureLabel = if ($Chamber -eq "ombe") { "O-MBE" } else { "Ch-MBE" }
    Show-LaunchMessage $message "AI4MBE $failureLabel launch failed" $true
    Write-Error $message
    exit 1
}
