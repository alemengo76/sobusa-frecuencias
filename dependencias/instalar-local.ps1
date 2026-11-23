param([string]$Python = 'python', [string]$Ruedas)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path $PSScriptRoot
$origen = @('--index-url', 'https://pypi.org/simple')
if ($Ruedas) {
    $origen = @('--no-index', '--find-links', (Resolve-Path -LiteralPath $Ruedas).Path)
}
& $Python -c "import sys,struct,platform; sys.exit(not (sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.machine().upper()=='AMD64' and sys.platform=='win32'))"
if ($LASTEXITCODE -ne 0) { throw 'Se requiere Python 3.12 para Windows AMD64.' }

foreach ($entorno in @('backend', 'worker')) {
    $destino = Join-Path $raiz ".venv-$entorno"
    if (Test-Path -LiteralPath $destino) { throw "El entorno $destino ya existe." }
}
foreach ($entorno in @('backend', 'worker')) {
    $destino = Join-Path $raiz ".venv-$entorno"
    $lock = Join-Path $PSScriptRoot "$entorno-win-amd64-py312.lock"
    & $Python -m venv $destino
    if ($LASTEXITCODE -ne 0) { throw "Falló la creación de $entorno." }
    $exe = Join-Path $destino 'Scripts/python.exe'
    & $exe -m pip --isolated install --no-cache-dir --only-binary=:all: --require-hashes @origen -r $lock
    if ($LASTEXITCODE -ne 0) { throw "Falló la instalación de $entorno." }
    & $exe -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Dependencias incompatibles en $entorno." }
}
