param(
    [Parameter(Mandatory = $true)]
    [string]$ClientId,

    [switch]$Write
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonCommand = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCommand = "py"
    $pythonArgs = @("-3.14")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommand = "python"
} else {
    throw "Python 3.14 was not found. Install Python 3.14 and run this script again."
}

$pythonVersion = & $pythonCommand @pythonArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pythonVersion.Trim() -ne "3.14") {
    throw "Python 3.14 is required; found Python $pythonVersion."
}

$venvPath = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating local virtual environment with Python 3.14..."
    & $pythonCommand @pythonArgs -m venv $venvPath
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
