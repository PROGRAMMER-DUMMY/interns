<#
.SYNOPSIS
  Load .env into the process environment, then run the given command.

.DESCRIPTION
  Generic, CLI-agnostic env loader. The app (os.environ) and every CLI's MCP
  layer (.mcp.json ${VAR} expansion) read from the process environment, not from
  .env directly -- so this script is the single bridge for all of them.

  .env stays the one source of truth (gitignored). No tool-specific config needed.

.EXAMPLE
  scripts\with-env.ps1 claude
  scripts\with-env.ps1 gemini
  scripts\with-env.ps1 green-gate --sweep
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Command)

$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
      $k, $v = $line -split '=', 2
      [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
  }
  Write-Host "[ok] loaded .env into process environment"
} else {
  Write-Host "[~] no .env found (copy .env.example to .env first)"
}

if ($Command) {
  & $Command[0] @($Command[1..($Command.Length - 1)])
}
