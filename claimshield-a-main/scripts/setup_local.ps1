param(
    [string]$Python = "python",
    [string]$Venv = ".venv"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 'Python 3.11 is required')"
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.11 or pass -Python with its executable path." }

    $venvPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Venv))
    if (-not $venvPath.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Choose a virtual environment directory inside this repository."
    }
    & $Python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
    $venvPython = Join-Path $venvPath "Scripts\python.exe"
    & $venvPython -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed." }
    Write-Host "Setup complete. Run: $Venv\Scripts\python.exe -m pytest -q"
} finally {
    Pop-Location
}
