<#
.SYNOPSIS
  Destroy the throw-away stack and ALL its data (containers, networks, volumes).
#>
[CmdletBinding()]
param([switch]$KeepImage)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Removing containers, networks, and volumes..." -ForegroundColor Yellow
docker compose down -v --remove-orphans

if (-not $KeepImage) {
  Write-Host "Removing the built assistant image..." -ForegroundColor Yellow
  docker image rm paperless-assistant:reocr-test 2>$null | Out-Null
}

Write-Host "Done. The test stack is gone." -ForegroundColor Green
