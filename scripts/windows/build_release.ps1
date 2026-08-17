[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9A-Za-z][0-9A-Za-z._-]*$")]
    [string]$Version,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot "dist"
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw "Release packaging must run in 64-bit PowerShell on 64-bit Windows."
}
$gitSafety = "safe.directory=$repositoryRoot"
$commit = (& git -c $gitSafety -C $repositoryRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $commit) {
    throw "Could not resolve the source commit."
}
$dirty = (
    & git -c $gitSafety -C $repositoryRoot status --porcelain `
        --untracked-files=no 2>$null
) -join "`n"
if ($LASTEXITCODE -ne 0 -or $dirty.Trim()) {
    throw "Tracked files are dirty. Commit the intended release contents first."
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$stem = "AI4MBE-Growth-Monitor-$Version"
$scratch = Join-Path $OutputDirectory (".release-" + [guid]::NewGuid().ToString("N"))
$sourceZip = Join-Path $scratch "source.zip"
$packageRoot = Join-Path $scratch $stem
$releaseZip = Join-Path $OutputDirectory "$stem-Windows-x64.zip"
$checksumPath = "$releaseZip.sha256"

try {
    New-Item -ItemType Directory -Path $scratch -Force | Out-Null
    & git -c $gitSafety -C $repositoryRoot archive --format=zip --output=$sourceZip HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed."
    }
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
    Expand-Archive -LiteralPath $sourceZip -DestinationPath $packageRoot -Force
    # Ship only runtime/operator content. These paths remain in Git history,
    # but a workstation installer must not contain old experiment logs,
    # development metadata, tests, or the duplicated pre-release model bundle.
    $releaseExclusions = @(
        ".github",
        ".DS_Store",
        "CLAUDE.md",
        "pytest.ini",
        "tests",
        "tools\rheed_postprocessing_labeling\tests",
        "logs",
        "models\weak_primary_lambda_0_1\RHEEDClassify\Classifier2"
    )
    foreach ($relative in $releaseExclusions) {
        Remove-Item -LiteralPath (Join-Path $packageRoot $relative) `
            -Recurse -Force -ErrorAction SilentlyContinue
    }
    [ordered]@{
        schema_version = 1
        product_id = "AI4MBE.GrowthMonitor.Windows"
        architecture = "x64"
        version = $Version
        source_commit = $commit
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        install_entrypoint = "Install AI4MBE Growth Monitor.cmd"
        uninstall_entrypoint = "Uninstall AI4MBE Growth Monitor.cmd"
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (
        Join-Path $packageRoot ".ai4mbe-package.json"
    ) -Encoding UTF8

    Remove-Item -LiteralPath $releaseZip -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path $packageRoot -DestinationPath $releaseZip `
        -CompressionLevel Optimal
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseZip).Hash.ToLowerInvariant()
    "$hash  $(Split-Path -Leaf $releaseZip)" |
        Set-Content -LiteralPath $checksumPath -Encoding ASCII
    [ordered]@{
        version = $Version
        source_commit = $commit
        archive = $releaseZip
        sha256 = $hash
        bytes = (Get-Item -LiteralPath $releaseZip).Length
        checksum_file = $checksumPath
    } | ConvertTo-Json -Depth 4
}
finally {
    if (Test-Path -LiteralPath $scratch) {
        Remove-Item -LiteralPath $scratch -Recurse -Force
    }
}
