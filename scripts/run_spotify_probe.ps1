param(
    [Parameter(Mandatory = $true)]
    [string]$ClientId,

    [switch]$Write
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonLauncher = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonLauncher = "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonLauncher = "python"
} else {
    throw "Python was not found. Install Python 3.12+ and run this script again."
}

$venvPath = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating local virtual environment..."
    & $pythonLauncher -m venv $venvPath
}

Write-Host "Installing the local project..."
& $venvPython -m pip install --disable-pip-version-check -e .

$env:SPOTIFY_CLIENT_ID = $ClientId
try {
    $probeArgs = @("-m", "news_bulletin_playlist.spotify.oauth_probe")
    if ($Write) {
        $probeArgs += "--write"
    }

    Write-Host "Starting Spotify PKCE authorization..."
    & $venvPython @probeArgs
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:SPOTIFY_CLIENT_ID -ErrorAction SilentlyContinue
}
