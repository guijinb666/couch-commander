# 把 tools/ 里的自检脚本跑一遍（零副作用，不需要手机）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\run-all-checks.ps1
#
# 需要正在运行的服务的那几个脚本（_served_check / _fix_check / _e2e_keys /
# _daemon_check）本脚本不跑，它们会自己起临时端口。

$ErrorActionPreference = 'Continue'
$root   = Split-Path -Parent $PSScriptRoot
$tools  = Join-Path $root 'tools'
$server = Join-Path $root 'server'
$env:PYTHONIOENCODING = 'utf-8'

$pass   = 0
$fail   = 0
$failed = @()

function Run-Step {
    param([string]$Name, [string]$WorkDir, [scriptblock]$Body)
    Write-Host ("`n=== {0} ===" -f $Name) -ForegroundColor Cyan
    Push-Location $WorkDir
    try {
        & $Body
        if ($LASTEXITCODE -eq 0) {
            Write-Host ("--- {0}: OK" -f $Name) -ForegroundColor Green
            $script:pass++
        } else {
            Write-Host ("--- {0}: FAILED (exit {1})" -f $Name, $LASTEXITCODE) -ForegroundColor Red
            $script:fail++
            $script:failed += $Name
        }
    } finally {
        Pop-Location
    }
}

Write-Host "Couch Commander - check suite" -ForegroundColor Yellow
Write-Host ("interpreter: " + (python --version 2>&1))

Run-Step 'py_compile (server/*.py)' $server {
    $files = Get-ChildItem *.py | Select-Object -ExpandProperty Name
    python -m py_compile @files 2>&1 | ForEach-Object { Write-Host $_ }
}

Run-Step 'node --check (web/js/*.js)' $root {
    $bad = 0
    Get-ChildItem 'web\js' -Filter *.js | ForEach-Object {
        node --check $_.FullName 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $bad++; Write-Host ("BAD " + $_.Name) }
    }
    Write-Host ("checked files, bad={0}" -f $bad)
    if ($bad -eq 0) { $global:LASTEXITCODE = 0 } else { $global:LASTEXITCODE = 1 }
}

Run-Step 'selftest --offline'        $server { python selftest.py --offline }
Run-Step '_bugfix_check'             $tools  { python _bugfix_check.py }
Run-Step '_keymap_check'             $tools  { python _keymap_check.py }
Run-Step '_integration_check_v2'     $tools  { python _integration_check_v2.py }
Run-Step '_voicews_check'            $tools  { python _voicews_check.py }
Run-Step '_outbox_probe'             $tools  { python _outbox_probe.py }
Run-Step '_leak_probe'               $tools  { python _leak_probe.py }
Run-Step '_net_state.mjs'            $tools  { node _net_state.mjs }

Write-Host "`n============================================================" -ForegroundColor Yellow
Write-Host ("SUMMARY  pass={0}  fail={1}" -f $pass, $fail) -ForegroundColor Yellow
if ($fail -gt 0) { Write-Host ("FAILED: " + ($failed -join ', ')) -ForegroundColor Red }
Write-Host "============================================================" -ForegroundColor Yellow
if ($fail -gt 0) { exit 1 } else { exit 0 }
