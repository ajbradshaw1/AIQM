[CmdletBinding()]
param(
    [string]$PythonPath,

    # Resolve and describe the launch without opening Qt.
    [switch]$DryRun,

    # Dry-run dependency probing is opt-in so offline tests stay fast.
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

function Resolve-LabelerPython {
    param([string]$RepositoryRoot, [bool]$SkipProbe)

    $seen = @{}
    $failures = New-Object System.Collections.Generic.List[string]
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
            }
        }

        $probe = Invoke-NativeCapture $resolved @(
            "-I",
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); " +
                "import PyQt6, numpy, PIL; " +
                "import tools.rheed_postprocessing_labeling.desktop_launcher"
            ),
            $RepositoryRoot
        )
        if ($probe.ExitCode -eq 0) {
            return [pscustomobject]@{
                Path = $resolved
                Source = $candidate.Source
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
        "Could not find a 64-bit GUI Python with PyQt6, numpy, Pillow, " +
        "and the offline labeler.$details`nUse the repository .venv, activate " +
        "ai4mbe-gui, or set AI4MBE_GUI_PYTHON to its python.exe."
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
    param([string]$Text, [string]$Title)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show(
            $Text,
            $Title,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
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
    $entry = Join-Path $repositoryRoot (
        "tools\rheed_postprocessing_labeling\__main__.py"
    )
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "Offline labeler entry point is missing: $entry"
    }

    foreach ($variable in $sanitizedVariables) {
        Remove-Item -LiteralPath "Env:$variable" -ErrorAction SilentlyContinue
    }
    $env:PYTHONNOUSERSITE = "1"
    $env:PIP_USER = "no"

    $skipProbe = [bool]$DryRun -and -not [bool]$ProbeCandidates
    $python = Resolve-LabelerPython $repositoryRoot $skipProbe
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $localAppData) {
        $localAppData = $env:TEMP
    }
    $logDirectory = Join-Path $localAppData "AI4MBE\LauncherLogs"
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $arguments = @(
        "-m", "tools.rheed_postprocessing_labeling", "desktop"
    )

    $launchPlan = [ordered]@{
        dry_run = [bool]$DryRun
        application = "labeler"
        repository_root = $repositoryRoot
        working_directory = $repositoryRoot
        entry_point = $entry
        python = $python.Path
        python_source = $python.Source
        git_branch = Get-GitValue $repositoryRoot @(
            "symbolic-ref", "--short", "-q", "HEAD"
        )
        git_commit = Get-GitValue $repositoryRoot @("rev-parse", "HEAD")
        arguments = $arguments
        chamber = $null
        python_no_user_site = $env:PYTHONNOUSERSITE
        sanitized_variables = $sanitizedVariables
        log_directory = $logDirectory
    }
    if ($DryRun) {
        $launchPlan | ConvertTo-Json -Depth 5
        exit 0
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logStem = Join-Path $logDirectory "labeler-$timestamp"
    $metadataLog = "$logStem-launch.json"
    $stdoutLog = "$logStem-stdout.log"
    $stderrLog = "$logStem-stderr.log"
    $launchPlan | ConvertTo-Json -Depth 5 | Set-Content `
        -LiteralPath $metadataLog -Encoding UTF8

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
        throw (
            "RHEED Post-processing Labeler exited with code " +
            "$($process.ExitCode).`nError log: $stderrLog"
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
            "labeler-launcher-failure-" +
            (Get-Date -Format "yyyyMMdd_HHmmss_fff") + ".log"
        )
        $message | Set-Content -LiteralPath $failureLog -Encoding UTF8
        $message = "$message`n`nLauncher log: $failureLog"
    }
    catch {
        # The dialog still contains the original failure when logging fails.
    }
    Show-LaunchMessage $message "AI4MBE labeler launch failed"
    Write-Error $message
    exit 1
}
