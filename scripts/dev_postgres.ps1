param(
    [ValidateSet('start', 'stop', 'status')][string]$Action = 'start',
    [ValidateRange(1024, 65535)][int]$Port = 55433
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$runtimeRoot = Join-Path $repoRoot '.runtime'
$postgresRoot = Join-Path $runtimeRoot 'postgres\pgsql'
$dataPath = Join-Path $runtimeRoot 'pgdata'
$pgCtl = Join-Path $postgresRoot 'bin\pg_ctl.exe'
$passwordPath = Join-Path $runtimeRoot 'postgres.password'
$environmentPath = Join-Path $runtimeRoot 'db.env'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

if ($Action -ne 'start') {
    if (-not (Test-Path -LiteralPath $pgCtl)) { throw 'PostgreSQL has not been bootstrapped.' }
    & $pgCtl -D $dataPath $Action
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $pgCtl)) {
    $archivePath = Join-Path $runtimeRoot 'postgres.zip'
    if (-not (Test-Path -LiteralPath $archivePath)) {
        # The archive is linked from https://www.enterprisedb.com/download-postgresql-binaries.
        # Python uses its TLS stack, which also works in Windows restricted-token environments.
        @'
import pathlib
import sys
import urllib.request

archive = pathlib.Path(sys.argv[1])
partial = archive.with_suffix('.zip.part')
urllib.request.urlretrieve('https://get.enterprisedb.com/postgresql/postgresql-18.6-5-windows-x64-binaries.zip', partial)
partial.replace(archive)
'@ | & python - $archivePath
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL download failed.' }
    }
    @'
import pathlib
import sys
import zipfile

root = pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(sys.argv[1]) as archive:
    members = [name for name in archive.namelist() if name.startswith(('pgsql/bin/', 'pgsql/lib/', 'pgsql/share/'))]
    assert members
    assert all((root / name).resolve().is_relative_to(root) for name in members)
    archive.extractall(root, members)
'@ | & python - $archivePath (Join-Path $runtimeRoot 'postgres')
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL extraction failed.' }
}

if (-not (Test-Path -LiteralPath (Join-Path $dataPath 'PG_VERSION'))) {
    if (Test-Path -LiteralPath $passwordPath) { throw 'Existing password with no database: inspect .runtime before retrying.' }
    $secretBytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($secretBytes) } finally { $generator.Dispose() }
    $databaseSecret = [BitConverter]::ToString($secretBytes).Replace('-', '').ToLowerInvariant()
    [IO.File]::WriteAllText($passwordPath, $databaseSecret + "`n")
    & (Join-Path $postgresRoot 'bin\initdb.exe') -D $dataPath -U postgres --auth=scram-sha-256 --encoding=UTF8 --locale=C --pwfile=$passwordPath
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL cluster initialization failed.' }
    Add-Content -LiteralPath (Join-Path $dataPath 'postgresql.conf') -Value "`nlisten_addresses = '127.0.0.1'`nport = $Port`n"
}

& $pgCtl -D $dataPath status *> $null
if ($LASTEXITCODE -ne 0) {
    # pg_ctl start attempts token elevation changes that fail inside restricted Windows
    # environments. Launch postgres directly, hidden, without registering a system service.
    $argumentLine = '-D "{0}" -p {1}' -f $dataPath, $Port
    $launch = Start-Process -FilePath (Join-Path $postgresRoot 'bin\postgres.exe') -ArgumentList $argumentLine -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeRoot 'postgres.out') -RedirectStandardError (Join-Path $runtimeRoot 'postgres.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $launch.Refresh()
        if ($launch.HasExited) { break }
        & (Join-Path $postgresRoot 'bin\pg_isready.exe') -h 127.0.0.1 -p $Port -q
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'PostgreSQL failed to start. See .runtime/postgres.log.' }
} else {
    $runningPort = [int]([IO.File]::ReadAllLines((Join-Path $dataPath 'postmaster.pid'))[3])
    if ($runningPort -ne $Port) { throw "This cluster already runs on port $runningPort. Use -Port $runningPort or stop it before changing ports." }
}

$databaseSecret = (Get-Content -LiteralPath $passwordPath -Raw).Trim()
$oldPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = $databaseSecret
    foreach ($databaseName in @('downloadbot', 'downloadbot_test')) {
        $exists = & (Join-Path $postgresRoot 'bin\psql.exe') -h 127.0.0.1 -p $Port -U postgres -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname = '$databaseName'"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot connect to local PostgreSQL.' }
        if ($exists -ne '1') {
            & (Join-Path $postgresRoot 'bin\createdb.exe') -h 127.0.0.1 -p $Port -U postgres $databaseName
            if ($LASTEXITCODE -ne 0) { throw "Could not create $databaseName." }
        }
    }
} finally {
    $env:PGPASSWORD = $oldPassword
}
$connectionRoot = 'postgresql://postgres:{0}@127.0.0.1:{1}/' -f $databaseSecret, $Port
[IO.File]::WriteAllText($environmentPath, "DOWNLOADBOT_DATABASE_URL=${connectionRoot}downloadbot`nDOWNLOADBOT_TEST_DATABASE_URL=${connectionRoot}downloadbot_test`n")
Write-Output "PostgreSQL is ready on 127.0.0.1:$Port. Connection settings: .runtime/db.env (gitignored)."
