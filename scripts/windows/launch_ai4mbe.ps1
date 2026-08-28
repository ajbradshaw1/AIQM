[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("ombe", "chmbe", "labeler")]
    [string]$Application,

    [string]$PythonPath,

    # Resolve and describe the launch without opening Qt or hardware.
    [switch]$DryRun,

    # Test/support switches: probe dependencies during a dry run, override the
    # Conda executable, or relocate the managed-workstation fallback.
    [switch]$ProbeCandidates,
    [string]$CondaExecutable,
    [string]$ManagedEnvironmentPath = (
        "D:\Environment_Cache\conda_envs\ai4mbe-gui\python.exe"
    )
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# Windows PowerShell otherwise chooses a legacy console code page when stdout
# is redirected, corrupting non-ASCII user/profile and repository paths.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Get-RepositoryRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
}

function Add-PythonCandidate {
    param(
        [System.Collections.ArrayList]$Candidates,
        [string]$Path,
        [string]$Source
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    [void]$Candidates.Add([pscustomobject]@{ Path = $Path; Source = $Source })
}

function Get-FastPythonCandidates {
    param(
        [string]$RepositoryRoot,
        [string]$ExplicitPath,
        [string]$ManagedPath
    )

    $candidates = New-Object System.Collections.ArrayList
    Add-PythonCandidate $candidates $ExplicitPath "-PythonPath"
    Add-PythonCandidate $candidates $env:AI4MBE_GUI_PYTHON "AI4MBE_GUI_PYTHON"
    Add-PythonCandidate $candidates `
        (Join-Path $RepositoryRoot ".venv\Scripts\python.exe") `
        "repository .venv"
    if ($env:USERPROFILE) {
        Add-PythonCandidate $candidates `
            (Join-Path $env:USERPROFILE (
                "AIQM-Software-Hardware-Integration\.venv\Scripts\python.exe"
            )) `
            "shared lab repository .venv"
    }
    if ($env:CONDA_PREFIX) {
        Add-PythonCandidate $candidates `
            (Join-Path $env:CONDA_PREFIX "python.exe") `
            "active CONDA_PREFIX"
    }

    if ($env:USERPROFILE) {
        $registeredEnvironments = Join-Path `
            $env:USERPROFILE ".conda\environments.txt"
        if (Test-Path -LiteralPath $registeredEnvironments -PathType Leaf) {
            foreach ($prefix in Get-Content -LiteralPath $registeredEnvironments) {
                $trimmed = $prefix.Trim()
                if (-not $trimmed) {
                    continue
                }
                $leaf = Split-Path -Leaf $trimmed.TrimEnd("\", "/")
                if ($leaf -ieq "ai4mbe-gui") {
                    Add-PythonCandidate $candidates `
                        (Join-Path $trimmed "python.exe") `
                        "registered Conda environment"
                }
            }
        }
    }
    # The environment-management scripts provision this prefix on the AI4MBE
    # development workstation. Keep it after portable/registered discovery so
    # another workstation can relocate the environment without editing code.
    Add-PythonCandidate $candidates $ManagedPath "managed workstation fallback"

    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pathPython) {
        Add-PythonCandidate $candidates $pathPython.Source "PATH"
    }
    return $candidates
}

function Get-CondaPythonCandidates {
    param([string]$RequestedConda)

    $candidates = New-Object System.Collections.ArrayList
    $condaPath = $RequestedConda
    if (-not $condaPath) {
        $conda = Get-Command conda -ErrorAction SilentlyContinue
        if ($conda) {
            $condaPath = $conda.Source
        }
    }
    if ($condaPath -and (Test-Path -LiteralPath $condaPath -PathType Leaf)) {
        $jsonText = ""
        $condaExit = -1
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $jsonText = (& $condaPath env list --json 2>$null) | Out-String
            $condaExit = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($condaExit -eq 0 -and $jsonText.Trim()) {
            try {
                $environmentList = $jsonText | ConvertFrom-Json
                foreach ($prefix in $environmentList.envs) {
                    $leaf = Split-Path -Leaf ([string]$prefix).TrimEnd("\", "/")
                    if ($leaf -ieq "ai4mbe-gui") {
                        Add-PythonCandidate $candidates `
                            (Join-Path ([string]$prefix) "python.exe") `
                            "conda env list"
                    }
                }
            }
            catch {
                # A noisy or older Conda command is not authoritative; later
                # candidates can still provide a verified interpreter.
            }
        }
    }
    return $candidates
}

function Invoke-NativeCapture {
    param([string]$Executable, [string[]]$Arguments)
    $output = @()
    $exitCode = -1
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5 promotes native stderr to ErrorRecord objects.
        # The native exit code remains the authority for an import probe.
        $ErrorActionPreference = "Continue"
        $output = & $Executable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = (($output | ForEach-Object { "$_" }) -join [Environment]::NewLine)
    }
}

function Find-CompatiblePythonCandidate {
    param(
        [object[]]$Candidates,
        [string]$ApplicationName,
        [bool]$SkipProbe,
        [hashtable]$Seen,
        [System.Collections.Generic.List[string]]$Failures
    )
    foreach ($candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Path -PathType Leaf)) {
            if ($candidate.Source -in @("-PythonPath", "AI4MBE_GUI_PYTHON")) {
                [void]$Failures.Add(
                    "$($candidate.Source): file not found: $($candidate.Path)"
                )
            }
            continue
        }
        $resolved = (Resolve-Path -LiteralPath $candidate.Path).Path
        $key = $resolved.ToLowerInvariant()
        if ($Seen.ContainsKey($key)) {
            continue
        }
        $Seen[$key] = $true
        if ($SkipProbe) {
            return [pscustomobject]@{ Path = $resolved; Source = $candidate.Source }
        }

        $imports = if ($ApplicationName -in @("ombe", "chmbe")) {
            # Match growth_monitor_app.py's Windows DLL rule: torch must load
            # its Intel OpenMP runtime before PyQt6 attempts plugin loading.
            "import struct; assert struct.calcsize('P') == 8; import torch; import PyQt6, numpy, PIL"
        }
        else {
            "import struct; assert struct.calcsize('P') == 8; import PyQt6, numpy, PIL"
        }
        $probe = Invoke-NativeCapture $resolved @("-I", "-c", $imports)
        if ($probe.ExitCode -eq 0) {
            return [pscustomobject]@{ Path = $resolved; Source = $candidate.Source }
        }
        $summary = ([string]$probe.Output).Trim()
        if ($summary.Length -gt 500) {
            $summary = $summary.Substring(0, 500) + "..."
        }
        [void]$Failures.Add("$($candidate.Source): $resolved`n$summary")
    }
    return $null
}

function Resolve-Ai4mbePython {
    param(
        [string]$RepositoryRoot,
        [string]$RequestedPath,
        [string]$ApplicationName,
        [bool]$SkipProbe,
        [string]$RequestedConda,
        [string]$ManagedPath
    )
    $seen = @{}
    $failures = New-Object System.Collections.Generic.List[string]
    $fastCandidates = Get-FastPythonCandidates `
        $RepositoryRoot $RequestedPath $ManagedPath
    $selected = Find-CompatiblePythonCandidate `
        $fastCandidates $ApplicationName $SkipProbe $seen $failures
    if ($null -ne $selected) {
        return $selected
    }

    # Conda enumeration is deliberately phase two. It is skipped on the
    # normal fast path, but a stale executable that merely exists cannot hide
    # a valid registered ai4mbe-gui environment returned by Conda.
    $condaCandidates = Get-CondaPythonCandidates $RequestedConda
    $selected = Find-CompatiblePythonCandidate `
        $condaCandidates $ApplicationName $SkipProbe $seen $failures
    if ($null -ne $selected) {
        return $selected
    }

    $details = if ($failures.Count) {
        "`nCandidates rejected:`n" + ($failures -join "`n---`n")
    }
    else {
        ""
    }
    throw "Could not find an ai4mbe-gui Python interpreter with the required packages.$details`nSet AI4MBE_GUI_PYTHON to that environment's python.exe."
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
    $value = @()
    $gitExit = -1
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $value = & $git.Source -C $RepositoryRoot @Arguments 2>$null
        $gitExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($gitExit -ne 0) {
        return ""
    }
    return (($value | ForEach-Object { "$_" }) -join "").Trim()
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
    $repositoryRoot = Get-RepositoryRoot
    $installMarkerPath = Join-Path $repositoryRoot ".ai4mbe-install.json"
    $installedDataRoot = $null
    if (Test-Path -LiteralPath $installMarkerPath -PathType Leaf) {
        try {
            $installMarker = Get-Content -Raw -LiteralPath $installMarkerPath |
                ConvertFrom-Json
            if (
                $installMarker.product_id -eq "AI4MBE.GrowthMonitor.Windows" -and
                $installMarker.data_root
            ) {
                $installedDataRoot = [string]$installMarker.data_root
                $env:AIQM_SESSION_ROOT = $installedDataRoot
            }
        }
        catch {
            throw "Installed application metadata is invalid: $installMarkerPath"
        }
    }
    foreach ($variable in $sanitizedVariables) {
        Remove-Item -LiteralPath "Env:$variable" -ErrorAction SilentlyContinue
    }
    $env:PYTHONNOUSERSITE = "1"
    $env:PIP_USER = "no"
    if ($Application -eq "chmbe") {
        # Do not trust a persistent machine/user setting. This must happen
        # before any Python module can cache drivers.config.
        $env:AIQM_CHAMBER = "chmbe"
        $arguments = @("growth_monitor_chmbe.py")
        $entry = Join-Path $repositoryRoot "growth_monitor_chmbe.py"
    }
    elseif ($Application -eq "ombe") {
        # The O-MBE shortcut must be equally immune to a stale Ch-MBE
        # workstation environment variable.
        $env:AIQM_CHAMBER = "ombe"
        $arguments = @("growth_monitor_ombe.py")
        $entry = Join-Path $repositoryRoot "growth_monitor_ombe.py"
    }
    else {
        $arguments = @("-m", "tools.rheed_postprocessing_labeling", "desktop")
        $entry = Join-Path $repositoryRoot "tools\rheed_postprocessing_labeling\__main__.py"
    }
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "Application entry point is missing: $entry"
    }

    $skipProbe = [bool]$DryRun -and -not [bool]$ProbeCandidates
    $python = Resolve-Ai4mbePython `
        $repositoryRoot $PythonPath $Application $skipProbe `
        $CondaExecutable $ManagedEnvironmentPath
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $localAppData) {
        $localAppData = $env:TEMP
    }
    $logDirectory = Join-Path $localAppData "AI4MBE\LauncherLogs"
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $logStem = Join-Path $logDirectory "$Application-$timestamp"

    $launchPlan = [ordered]@{
        dry_run = [bool]$DryRun
        application = $Application
        repository_root = $repositoryRoot
        working_directory = $repositoryRoot
        python = $python.Path
        python_source = $python.Source
        git_branch = Get-GitValue $repositoryRoot @(
            "symbolic-ref", "--short", "-q", "HEAD"
        )
        git_commit = Get-GitValue $repositoryRoot @("rev-parse", "HEAD")
        arguments = $arguments
        chamber = if ($Application -in @("ombe", "chmbe")) {
            $env:AIQM_CHAMBER
        }
        else {
            $null
        }
        python_no_user_site = $env:PYTHONNOUSERSITE
        sanitized_variables = $sanitizedVariables
        log_directory = $logDirectory
        session_root = $installedDataRoot
        mutex_name = if ($Application -eq "chmbe") {
            "Local\AI4MBE.ChMBE.GrowthMonitor"
        }
        elseif ($Application -eq "ombe") {
            "Local\AI4MBE.OMBE.GrowthMonitor"
        }
        else {
            $null
        }
    }
    if ($DryRun) {
        $launchPlan | ConvertTo-Json -Depth 5
        exit 0
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $metadataLog = "$logStem-launch.json"
    $stdoutLog = "$logStem-stdout.log"
    $stderrLog = "$logStem-stderr.log"
    $optionalDrivers = [ordered]@{}
    if ($Application -in @("ombe", "chmbe")) {
        foreach ($module in @("pyads", "serial", "windows_capture", "vmbpy")) {
            $driverProbe = Invoke-NativeCapture $python.Path @(
                "-I", "-c", "import $module"
            )
            $optionalDrivers[$module] = if ($driverProbe.ExitCode -eq 0) {
                "available"
            }
            else {
                "missing (the corresponding live instrument mode will fail)"
            }
        }
    }
    $launchPlan["optional_driver_imports"] = $optionalDrivers
    $launchPlan | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $metadataLog -Encoding UTF8
    $missingDrivers = @(
        $optionalDrivers.Keys | Where-Object {
            $optionalDrivers[$_] -ne "available"
        }
    )
    if ($missingDrivers.Count -gt 0) {
        $driverList = ($missingDrivers | ForEach-Object { "  - $_" }) -join "`n"
        Show-LaunchMessage (
            "Some optional $Application live drivers are unavailable:`n`n" +
            "$driverList`n`nThe GUI will still open. Dummy and unaffected " +
            "direct modes remain usable; selecting a mode backed by a missing " +
            "module will report an instrument error.`n`nDetails: $metadataLog"
        ) "AI4MBE live-driver warning" $false
    }

    if ($Application -in @("ombe", "chmbe")) {
        $expectedChamber = $Application
        $displayChamber = if ($Application -eq "chmbe") { "Ch-MBE" } else { "O-MBE" }
        $chamberProbe = @(
            "-I", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from drivers.config import get_active_config; assert get_active_config().chamber_id == sys.argv[2]",
            $repositoryRoot,
            $expectedChamber
        )
        $probe = Invoke-NativeCapture $python.Path $chamberProbe
        if ($probe.ExitCode -ne 0) {
            throw "$displayChamber chamber preflight failed: $($probe.Output)"
        }
    }

    $process = Start-Process `
        -FilePath $python.Path `
        -ArgumentList $arguments `
        -WorkingDirectory $repositoryRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Application exited with code $($process.ExitCode).`nError log: $stderrLog"
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
            "launcher-failure-" + (Get-Date -Format "yyyyMMdd_HHmmss_fff") + ".log"
        )
        $message | Set-Content -LiteralPath $failureLog -Encoding UTF8
        $message = "$message`n`nLauncher log: $failureLog"
    }
    catch {
        # The dialog still contains the original failure when logging fails.
    }
    Show-LaunchMessage $message "AI4MBE launch failed" $true
    Write-Error $message
    exit 1
}
