[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$manualRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $manualRoot '..\..\..')).Path
$source = Join-Path $manualRoot 'RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.tex'
$buildRoot = Join-Path $repositoryRoot 'build\rheed-manual-english'
$builtPdf = Join-Path $buildRoot 'RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf'
$finalPdf = Join-Path $repositoryRoot 'docs\RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf'

$pdflatex = Get-Command 'pdflatex.exe' -ErrorAction SilentlyContinue
if ($null -eq $pdflatex) {
    $pdflatex = Get-Command 'pdflatex' -ErrorAction SilentlyContinue
}
if ($null -eq $pdflatex) {
    throw 'pdfLaTeX was not found. Install TeX Live with pdfLaTeX.'
}

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$previousSourceDateEpoch = $env:SOURCE_DATE_EPOCH
$previousForceSourceDate = $env:FORCE_SOURCE_DATE
try {
    $env:SOURCE_DATE_EPOCH = '1786406400'
    $env:FORCE_SOURCE_DATE = '1'
    Push-Location $manualRoot
    try {
        foreach ($pass in 1..3) {
            & $pdflatex.Source '-interaction=nonstopmode' '-halt-on-error' `
                '-file-line-error' "-output-directory=$buildRoot" $source
            if ($LASTEXITCODE -ne 0) {
                throw "LaTeX pass $pass failed with exit code $LASTEXITCODE."
            }
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $previousSourceDateEpoch) {
        Remove-Item Env:SOURCE_DATE_EPOCH -ErrorAction SilentlyContinue
    }
    else {
        $env:SOURCE_DATE_EPOCH = $previousSourceDateEpoch
    }
    if ($null -eq $previousForceSourceDate) {
        Remove-Item Env:FORCE_SOURCE_DATE -ErrorAction SilentlyContinue
    }
    else {
        $env:FORCE_SOURCE_DATE = $previousForceSourceDate
    }
}

if (-not (Test-Path -LiteralPath $builtPdf -PathType Leaf)) {
    throw "Expected PDF was not produced: $builtPdf"
}

Copy-Item -LiteralPath $builtPdf -Destination $finalPdf -Force
Write-Output $finalPdf
