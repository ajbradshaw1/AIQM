[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$InstallRoot,
    [string]$DataRoot,
    [string]$PythonPath,
    [switch]$SkipDependencyInstall,
    [switch]$NoShortcuts,
    [switch]$Quiet,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$productId = "AI4MBE.GrowthMonitor.Windows"
$markerName = ".ai4mbe-install.json"
$requiredFiles = @(
    "growth_monitor_ombe.py",
    "growth_monitor_chmbe.py",
    "requirements-installer.txt",
    "scripts\windows\launch_ai4mbe.ps1",
    "scripts\windows\install_shortcuts.ps1",
    "scripts\windows\uninstall_ai4mbe.ps1"
)

function Show-InstallerMessage {
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
        Output = (($output | ForEach-Object { "$_" }) -join [Environment]::NewLine)
    }
}

function Test-CompatiblePython {
    param([string]$Candidate)
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $probe = Invoke-NativeCapture $Candidate @(
        "-I", "-c",
        "import struct; assert struct.calcsize('P') == 8; import torch; import PyQt6, numpy, PIL"
    )
    return $probe.ExitCode -eq 0
}

function Get-PythonCandidates {
    param([string]$RequestedPath, [string]$RepositoryRoot)
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in @(
        $RequestedPath,
        $env:AI4MBE_GUI_PYTHON,
        (Join-Path $RepositoryRoot ".venv\Scripts\python.exe"),
        "D:\Environment_Cache\conda_envs\ai4mbe-gui\python.exe"
    )) {
        if ($candidate) {
            [void]$candidates.Add($candidate)
        }
    }
    if ($env:USERPROFILE) {
        $registered = Join-Path $env:USERPROFILE ".conda\environments.txt"
        if (Test-Path -LiteralPath $registered -PathType Leaf) {
            foreach ($prefix in Get-Content -LiteralPath $registered) {
                if ((Split-Path -Leaf $prefix.Trim().TrimEnd("\", "/")) -ieq "ai4mbe-gui") {
                    [void]$candidates.Add((Join-Path $prefix.Trim() "python.exe"))
                }
            }
        }
    }
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pathPython) {
        [void]$candidates.Add($pathPython.Source)
    }
    return $candidates | Select-Object -Unique
}

function Find-BootstrapPython {
    param([string]$RequestedPath)
    if ($RequestedPath -and (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
        $probe = Invoke-NativeCapture $RequestedPath @(
            "-c", "import struct,sys; assert struct.calcsize('P') == 8; assert (3,10) <= sys.version_info[:2] <= (3,12)"
        )
        if ($probe.ExitCode -eq 0) {
            return (Resolve-Path -LiteralPath $RequestedPath).Path
        }
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($selector in @("-3.12-64", "-3.11-64", "-3.10-64")) {
            $probe = Invoke-NativeCapture $py.Source @(
                $selector, "-c",
                "import struct,sys; assert struct.calcsize('P') == 8; print(sys.executable)"
            )
            if ($probe.ExitCode -eq 0) {
                return $probe.Output.Trim()
            }
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $probe = Invoke-NativeCapture $python.Source @(
            "-c", "import struct,sys; assert struct.calcsize('P') == 8; assert (3,10) <= sys.version_info[:2] <= (3,12)"
        )
        if ($probe.ExitCode -eq 0) {
            return $python.Source
        }
    }
    throw "64-bit Python 3.10, 3.11, or 3.12 was not found. Install 64-bit Python 3.12 and rerun this installer."
}

function Copy-ApplicationTree {
    param([string]$From, [string]$To)
    $excluded = @(
        ".git", ".venv", ".pytest_cache", ".codex-jobs", ".codex-scratch",
        "__pycache__", "build", "dist", "logs", "tmp"
    )
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $From -Force) {
        if ($item.Name -in $excluded) {
            continue
        }
        Copy-Item -LiteralPath $item.FullName -Destination $To -Recurse -Force
    }
}

function Test-PathInside {
    param([string]$Candidate, [string]$Parent)
    try {
        $candidateFull = [IO.Path]::GetFullPath($Candidate).TrimEnd("\") + "\"
        $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd("\") + "\"
        return $candidateFull.StartsWith(
            $parentFull, [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw (
            "This release requires 64-bit Windows. PyTorch, PyQt6, and the " +
            "validated WGC capture stack are not available as a supported " +
            "32-bit Windows combination. No files were installed."
        )
    }
    if (-not $SourceRoot) {
        $SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
    }
    else {
        $SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
    }
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $localAppData) {
        throw "Windows did not provide a LocalApplicationData folder."
    }
    if (-not $InstallRoot) {
        $InstallRoot = Join-Path $localAppData "Programs\AI4MBE-Growth-Monitor"
    }
    $documents = [Environment]::GetFolderPath("MyDocuments")
    if (-not $DataRoot) {
        $DataRoot = Join-Path $documents "AI4MBE\GrowthSessions"
    }
    $InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    foreach ($relative in $requiredFiles) {
        $required = Join-Path $SourceRoot $relative
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Installation package is incomplete. Missing: $relative"
        }
    }
    if ($InstallRoot.TrimEnd("\") -ieq $SourceRoot.TrimEnd("\")) {
        throw "The install destination must differ from the extracted package folder."
    }

    $compatiblePython = $null
    foreach ($candidate in Get-PythonCandidates $PythonPath $SourceRoot) {
        # An environment inside the previous installed tree would be deleted
        # during an update. Never bind the new shortcuts to that stale path.
        if (Test-PathInside $candidate $InstallRoot) {
            continue
        }
        if (Test-CompatiblePython $candidate) {
            $compatiblePython = (Resolve-Path -LiteralPath $candidate).Path
            break
        }
    }
    $willCreateEnvironment = -not $compatiblePython
    if ($willCreateEnvironment -and $SkipDependencyInstall) {
        throw "No compatible ai4mbe-gui Python was found and dependency installation was disabled."
    }

    $plan = [ordered]@{
        dry_run = [bool]$DryRun
        product_id = $productId
        architecture = "x64"
        source_root = $SourceRoot
        install_root = $InstallRoot
        data_root = $DataRoot
        python = $compatiblePython
        create_environment = $willCreateEnvironment
        preserve_on_uninstall = @(
            $DataRoot,
            (Join-Path $localAppData "AI4MBE\LauncherLogs")
        )
        create_shortcuts = -not [bool]$NoShortcuts
    }
    if ($DryRun) {
        $plan | ConvertTo-Json -Depth 5
        exit 0
    }

    $parent = Split-Path -Parent $InstallRoot
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $staging = Join-Path $parent (".AI4MBE-install-" + [guid]::NewGuid().ToString("N"))
    $backup = $null
    try {
        Copy-ApplicationTree $SourceRoot $staging
        if ($willCreateEnvironment) {
            $bootstrapPython = Find-BootstrapPython $PythonPath
            $venv = Join-Path $staging ".venv"
            & $bootstrapPython -m venv $venv
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to create the isolated Python environment."
            }
            $compatiblePython = Join-Path $venv "Scripts\python.exe"
            & $compatiblePython -m pip install --upgrade pip
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to update pip in the isolated environment."
            }
            & $compatiblePython -m pip install -r (Join-Path $staging "requirements-installer.txt")
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to install AI4MBE Python dependencies."
            }
            if (-not (Test-CompatiblePython $compatiblePython)) {
                throw "The new 64-bit Python environment failed its torch/PyQt6 import check."
            }
        }

        New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
        $installedPython = if ($willCreateEnvironment) {
            Join-Path $InstallRoot ".venv\Scripts\python.exe"
        }
        else {
            $compatiblePython
        }
        $marker = [ordered]@{
            schema_version = 1
            product_id = $productId
            architecture = "x64"
            installed_at_utc = [DateTime]::UtcNow.ToString("o")
            install_root = $InstallRoot
            data_root = $DataRoot
            python = $installedPython
            source_root = $SourceRoot
        }
        $marker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (
            Join-Path $staging $markerName
        ) -Encoding UTF8

        if (Test-Path -LiteralPath $InstallRoot) {
            $backup = "$InstallRoot.previous-" + (Get-Date -Format "yyyyMMddHHmmss")
            Move-Item -LiteralPath $InstallRoot -Destination $backup
        }
        Move-Item -LiteralPath $staging -Destination $InstallRoot
        $compatiblePython = $installedPython
        if ($backup) {
            Remove-Item -LiteralPath $backup -Recurse -Force
            $backup = $null
        }
    }
    catch {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
        if ($backup -and -not (Test-Path -LiteralPath $InstallRoot) -and (Test-Path -LiteralPath $backup)) {
            Move-Item -LiteralPath $backup -Destination $InstallRoot
        }
        throw
    }

    if (-not $NoShortcuts) {
        & (Join-Path $InstallRoot "scripts\windows\install_shortcuts.ps1") `
            -RepositoryRoot $InstallRoot -PythonPath $compatiblePython
        if ($LASTEXITCODE -ne 0) {
            throw "Application files were installed, but Desktop shortcut creation failed."
        }
    }
    if (-not $Quiet) {
        Show-InstallerMessage (
            "AI4MBE Growth Monitor is installed.`n`nProgram: $InstallRoot`n" +
            "Experiment data: $DataRoot`n`nUse the O-MBE or Ch-MBE Desktop shortcut."
        ) "AI4MBE installation complete" $false
    }
    exit 0
}
catch {
    $message = $_.Exception.Message
    $logRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "AI4MBE\InstallerLogs"
    try {
        New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        $log = Join-Path $logRoot ("install-" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
        $message | Set-Content -LiteralPath $log -Encoding UTF8
        $message = "$message`n`nInstaller log: $log"
    }
    catch {}
    Show-InstallerMessage $message "AI4MBE installation failed" $true
    Write-Error $message
    exit 1
}
