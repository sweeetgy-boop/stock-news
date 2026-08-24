# PowerShell 변형. run.cmd 와 동작이 같다.
# Hermes 가 pwsh 로 호출하는 편이 편할 때 쓴다.
#
#   hermes\run.ps1 --mode daily
#   hermes\run.ps1 --mode flash --json
#   hermes\run.ps1 smoke
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)

$ErrorActionPreference = 'Stop'

# 저장소 루트로 이동. 상대경로(data/quant.db)가 깨지지 않게 고정한다.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# UTF-8 강제. 콘솔 기본이 cp949 라 한글 stdout 이 깨진다.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 인터프리터 탐색. PATH 의 python 은 Microsoft Store 스텁일 수 있어 쓰지 않는다.
$candidates = @(
    (Join-Path $repo 'venv\Scripts\python.exe'),
    (Join-Path $repo '.venv\Scripts\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
    (Join-Path $env:ProgramFiles 'Python312\python.exe')
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { $py = $launcher.Source } else {
        Write-Error "파이썬을 찾을 수 없습니다. winget install --id Python.Python.3.12 --scope user"
        exit 91
    }
}

$rest = @($Args)
if ($rest.Count -ge 1 -and $rest[0] -eq 'smoke') {
    & $py smoke_test.py @($rest[1..($rest.Count - 1)])
} elseif ($rest.Count -ge 1 -and $rest[0] -eq 'verify') {
    & $py verify_env.py
} else {
    & $py run_screen.py @rest
}
exit $LASTEXITCODE
