[CmdletBinding()]
param(
    [string]$DesktopPath,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

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

try {
    $repositoryRoot = (Resolve-Path -LiteralPath (
        Join-Path $PSScriptRoot "..\.."
    )).Path
    if (-not $DesktopPath) {
        $DesktopPath = [Environment]::GetFolderPath("Desktop")
    }
    if (-not $DesktopPath) {
        throw "Windows did not provide a Desktop folder."
    }

    $systemRoot = $env:SystemRoot
    if (-not $systemRoot) {
        throw "Windows did not provide SystemRoot for shortcut icons."
    }
    $shellIcons = Join-Path $systemRoot "System32\shell32.dll"
    $powershell = Get-Command powershell.exe -ErrorAction Stop
    $launcher = Join-Path $repositoryRoot "scripts\windows\launch_ai4mbe.ps1"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Shared launcher is missing: $launcher"
    }
    $definitions = @(
        [ordered]@{
            Name = "Ch-MBE Growth Monitor"
            Target = $powershell.Source
            Arguments = (
                '-NoLogo -NoProfile -WindowStyle Hidden ' +
                '-ExecutionPolicy Bypass -File "' + $launcher +
                '" -Application chmbe'
            )
            TroubleshootingWrapper = Join-Path $repositoryRoot (
                "Start Ch-MBE Growth Monitor.cmd"
            )
            Description = "Start the AI4MBE Ch-MBE Growth Monitor"
            Icon = "$shellIcons,13"
        },
        [ordered]@{
            Name = "RHEED Post-processing Labeler"
            Target = $powershell.Source
            Arguments = (
                '-NoLogo -NoProfile -WindowStyle Hidden ' +
                '-ExecutionPolicy Bypass -File "' + $launcher +
                '" -Application labeler'
            )
            TroubleshootingWrapper = Join-Path $repositoryRoot (
                "Start RHEED Post-processing Labeler.cmd"
            )
            Description = "Build, open, and validate offline RHEED labeling reports"
            Icon = "$shellIcons,70"
        }
    )
    foreach ($definition in $definitions) {
        if (-not (
            Test-Path -LiteralPath $definition.TroubleshootingWrapper -PathType Leaf
        )) {
            throw "Troubleshooting wrapper is missing: $($definition.TroubleshootingWrapper)"
        }
        $definition["Shortcut"] = Join-Path $DesktopPath ($definition.Name + ".lnk")
    }

    if ($DryRun) {
        [ordered]@{
            dry_run = $true
            repository_root = $repositoryRoot
            desktop = $DesktopPath
            shortcuts = $definitions
        } | ConvertTo-Json -Depth 5
        exit 0
    }

    New-Item -ItemType Directory -Path $DesktopPath -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($definition in $definitions) {
            $shortcut = $shell.CreateShortcut($definition.Shortcut)
            $shortcut.TargetPath = $definition.Target
            $shortcut.Arguments = $definition.Arguments
            $shortcut.WorkingDirectory = $repositoryRoot
            $shortcut.Description = $definition.Description
            $shortcut.IconLocation = $definition.Icon
            $shortcut.Save()
        }
    }
    finally {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }

    $names = ($definitions | ForEach-Object { $_.Name }) -join "`n"
    Show-InstallerMessage (
        "Installed these Desktop shortcuts:`n`n$names`n`n" +
        "Rerun this installer after moving the repository."
    ) "AI4MBE shortcuts installed" $false
    exit 0
}
catch {
    $message = $_.Exception.Message
    Show-InstallerMessage $message "AI4MBE shortcut installation failed" $true
    Write-Error $message
    exit 1
}
