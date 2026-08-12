[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$DesktopPath,
    [switch]$RemoveApplicationFiles,
    [switch]$Quiet,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$productId = "AI4MBE.GrowthMonitor.Windows"
$shortcutNames = @(
    "O-MBE Growth Monitor.lnk",
    "Ch-MBE Growth Monitor.lnk",
    "RHEED Post-processing Labeler.lnk",
    "AI4MBE Operator Manual.lnk",
    "Uninstall AI4MBE Growth Monitor.lnk"
)

function Show-UninstallerMessage {
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
            $Text, $Title, [System.Windows.Forms.MessageBoxButtons]::OK, $icon
        )
    }
    catch { Write-Host $Text }
}

try {
    if (-not $InstallRoot) {
        $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
        $InstallRoot = Join-Path $localAppData "Programs\AI4MBE-Growth-Monitor"
        $RemoveApplicationFiles = $true
    }
    $InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
    if (-not $DesktopPath) {
        $DesktopPath = [Environment]::GetFolderPath("Desktop")
    }
    $markerPath = Join-Path $InstallRoot ".ai4mbe-install.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Refusing to remove an unverified directory. Install marker is missing: $markerPath"
    }
    $marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
    if ($marker.product_id -ne $productId) {
        throw "Refusing to remove a directory with an unknown product marker."
    }
    if ($marker.architecture -and $marker.architecture -ne "x64") {
        throw "Refusing to remove an installation for an unknown architecture."
    }
    if ([IO.Path]::GetFullPath([string]$marker.install_root).TrimEnd("\") -ine $InstallRoot.TrimEnd("\")) {
        throw "Install marker path does not match the requested uninstall directory."
    }

    $shortcuts = @($shortcutNames | ForEach-Object { Join-Path $DesktopPath $_ })
    $plan = [ordered]@{
        dry_run = [bool]$DryRun
        product_id = $productId
        architecture = if ($marker.architecture) { $marker.architecture } else { "legacy-unspecified" }
        install_root = $InstallRoot
        remove_application_files = [bool]$RemoveApplicationFiles
        shortcuts = $shortcuts
        preserved_data_root = [string]$marker.data_root
        preserved_launcher_logs = Join-Path (
            [Environment]::GetFolderPath("LocalApplicationData")
        ) "AI4MBE\LauncherLogs"
    }
    if ($DryRun) {
        $plan | ConvertTo-Json -Depth 4
        exit 0
    }

    foreach ($shortcut in $shortcuts) {
        Remove-Item -LiteralPath $shortcut -Force -ErrorAction SilentlyContinue
    }
    if ($RemoveApplicationFiles) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    if (-not $Quiet) {
        Show-UninstallerMessage (
            "AI4MBE Growth Monitor was removed.`n`nExperiment data was preserved at:`n" +
            [string]$marker.data_root
        ) "AI4MBE uninstallation complete" $false
    }
    exit 0
}
catch {
    $message = $_.Exception.Message
    Show-UninstallerMessage $message "AI4MBE uninstallation failed" $true
    Write-Error $message
    exit 1
}
