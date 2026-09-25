#Requires -Version 7.2
[CmdletBinding()]
param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$pythonCommand = (Get-Command $Python -ErrorAction Stop).Source
$root = Split-Path -Parent $PSScriptRoot

Push-Location -LiteralPath $root
try {
    $sources = foreach ($path in @('lab.ps1', 'tools\check_local.ps1')) {
        @{ Name = $path; Content = Get-Content -LiteralPath $path -Raw }
    }
    $readme = Get-Content -LiteralPath 'README.md' -Raw
    $snippets = [regex]::Matches($readme, '(?ms)^```powershell\r?\n(.*?)^```[ \t]*\r?$')
    foreach ($snippet in $snippets) {
        $sources += @{ Name = 'README PowerShell example'; Content = $snippet.Groups[1].Value }
    }
    foreach ($source in $sources) {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseInput(
            $source.Content, [ref]$tokens, [ref]$parseErrors
        ) | Out-Null
        if ($parseErrors.Count -gt 0) {
            throw "PowerShell parsing failed in $($source.Name): $($parseErrors -join '; ')"
        }
    }

    & $pythonCommand -m unittest discover -s .\tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Local unit tests failed.' }
    & $pythonCommand -m compileall -q .\lab.py .\guest .\tools .\tests
    if ($LASTEXITCODE -ne 0) { throw 'Python syntax checks failed.' }

    [IO.Directory]::CreateDirectory((Join-Path $root '.local\validation')) | Out-Null
    & az bicep build --file .\infra\main.bicep --outfile .\.local\validation\main.json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Local Bicep compilation failed.' }

    & $pythonCommand .\tools\package_release.py
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
    Write-Host 'Local checks passed; no live Azure actions or uploads were performed.'
}
finally {
    Pop-Location
}
