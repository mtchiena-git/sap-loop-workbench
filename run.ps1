# 啟動 SAP Loop Workbench（Windows PowerShell）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (Test-Path ".env") {
  Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*(#.*)?$') {
      $v = $Matches[2].Trim()
      if ($v -ne "") { [Environment]::SetEnvironmentVariable($Matches[1], $v, "Process") }
    }
  }
}
if (Test-Path ".venv\Scripts\python.exe") { $py = ".venv\Scripts\python.exe" } else { $py = "python" }
$port = if ($env:PORT) { $env:PORT } else { "8080" }
Write-Host "SAP Loop Workbench -> http://127.0.0.1:$port  (LLM=$($env:LLM_MODE) SAP=$($env:SAP_MODE) EXTRACT=$($env:EXTRACT_MODE))"
& $py -m uvicorn app:app --host 127.0.0.1 --port $port
