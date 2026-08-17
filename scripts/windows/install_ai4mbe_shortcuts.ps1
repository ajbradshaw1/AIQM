[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    $repositoryRoot = (
        Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
    ).Path
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop) {
        throw "Windows did not return a Desktop directory."
    }

    $definitions = @(
        [ordered]@{
            Name = "O-MBE Growth Monitor"
            Wrapper = "Start O-MBE Growth Monitor.cmd"
            Description = "Start the AI4MBE Oxide MBE Growth Monitor"
            Icon = "$env:SystemRoot\System32\shell32.dll,14"
        },
        [ordered]@{
            Name = "Ch-MBE Growth Monitor"
            Wrapper = "Start Ch-MBE Growth Monitor.cmd"
            Description = "Start the AI4MBE Chalcogenide MBE Growth Monitor"
            Icon = "$env:SystemRoot\System32\shell32.dll,14"
        },
        [ordered]@{
            Name = "RHEED Post-processing Labeler"
            Wrapper = "Start RHEED Post-processing Labeler.cmd"
            Description = "Open the offline RHEED point-event review tool"
            Icon = "$env:SystemRoot\System32\shell32.dll,70"
        }
    )

    $plan = foreach ($definition in $definitions) {
        $target = Join-Path $repositoryRoot $definition.Wrapper
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "Shortcut target is missing: $target"
        }
        [ordered]@{
            name = $definition.Name
            shortcut = Join-Path $desktop ($definition.Name + ".lnk")
            target = $target
            working_directory = $repositoryRoot
            description = $definition.Description
            icon = $definition.Icon
        }
    }

    if ($DryRun) {
        [ordered]@{
            dry_run = $true
            repository_root = $repositoryRoot
            desktop = $desktop
            shortcuts = @($plan)
        } | ConvertTo-Json -Depth 5
        exit 0
    }

    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($item in $plan) {
            $shortcut = $shell.CreateShortcut($item.shortcut)
            $shortcut.TargetPath = $item.target
            $shortcut.WorkingDirectory = $item.working_directory
            $shortcut.Description = $item.description
            $shortcut.IconLocation = $item.icon
            $shortcut.Save()
        }
    }
    finally {
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
    exit 0
}
catch {
    $message = $_.Exception.Message
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show(
            $message,
            "AI4MBE shortcut installation failed",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
    }
    catch {
        Write-Host $message
    }
    Write-Error $message
    exit 1
}
