# Canonical host integration. Configure the local scheduled-task launcher to call it.
param([string]$Scanner = '')
# The laptop stands in for the VPS until the move. Logs go to runs\dispatcher.log.
$repo = 'D:\showcase\agent-dispatcher'
$runs = Join-Path $repo 'runs'
$log = Join-Path $runs 'dispatcher.log'
# The project's own entry point; `uv sync --locked` keeps it current.
$exe = Join-Path $repo '.venv\Scripts\agent-dispatcher.exe'
New-Item -ItemType Directory -Force $runs | Out-Null
# Polling token only: gh's own login, never passed to the agent.
$env:DISPATCHER_GITHUB_TOKEN = (gh auth token)
Add-Content -Path $log -Value ("=== " + (Get-Date -Format o))
$env:PYTHONPATH = 'D:\showcase\platform-agent-runtime'
$sender = 'D:\showcase\platform-workflows\.venv\Scripts\python.exe'
# No model call for intake. Limit autonomous new work to one proposal per UTC day.
$intakeDay = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd')
$intakeState = 'D:\showcase\work\agent-intake-day.txt'
$lastIntake = if (Test-Path -LiteralPath $intakeState) { Get-Content -LiteralPath $intakeState -Raw } else { '' }
if ($lastIntake.Trim() -ne $intakeDay -and (Test-Path -LiteralPath 'D:\showcase\platform-agent-runtime\tools\ai_agent_cycle.py')) {
    $intakeResult = & $sender -m tools.ai_agent_cycle --apply
    if ($LASTEXITCODE -eq 0) {
        if (($intakeResult | ConvertFrom-Json).status -eq 'promoted') {
            Set-Content -LiteralPath $intakeState -Value $intakeDay
            Add-Content -Path $log -Value 'Verified autonomous audit proposal queued.'
        }
    } else { Add-Content -Path $log -Value 'Autonomous intake failed closed; no new authorization.' }
}
# Start-Process with files, not 2>&1: PowerShell 5.1 turns native stderr
# (uv's warnings) into errors, and Set-Location doesn't move child processes.
$out = Join-Path $runs 'last-stdout.log'
$err = Join-Path $runs 'last-stderr.log'
$process = Start-Process -FilePath $exe -ArgumentList 'supervise' `
    -WorkingDirectory $repo -RedirectStandardOutput $out -RedirectStandardError $err `
    -WindowStyle Hidden -Wait -PassThru
Get-Content $out, $err | Add-Content -Path $log
# Export only allowlisted, scrubbed public telemetry through reviewed platform code.
# Pending batches are acknowledged only after hosted signature verification.
if ($Scanner) { $env:AI_OUTPUT_SECRET_SCANNER = $Scanner }
Get-ChildItem -LiteralPath (Join-Path $runs 'telemetry') -Filter 'attempts-*.jsonl' -ErrorAction SilentlyContinue | ForEach-Object {
    & $sender -m tools.ai_agent_ingest submit --source $_.FullName --ledger 'D:\showcase\work\agent-telemetry-delivery.json'
    if ($LASTEXITCODE -ne 0) { Add-Content -Path $log -Value 'Telemetry export failed; records retained for retry.' }
}
exit $process.ExitCode

