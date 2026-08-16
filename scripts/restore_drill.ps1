# Restore drill — mahine mein ek baar chalao. Jo backup kabhi restore nahi
# kiya, us par bharosa mat karo.
#
# Kya karta hai: latest dump ko ek SCRATCH database mein restore karta hai
# (live 'laundry' DB ko haath bhi nahi lagata), core tables ke row counts
# compare karta hai, phir scratch DB gira deta hai.
#
# Zaroorat: postgres superuser ka password (CREATE DATABASE ke liye) —
# 'laundry' user ke paas CREATEDB nahi hai (jaan-boojh kar).
#
# Chalao:  powershell -File scripts\restore_drill.ps1

$ErrorActionPreference = "Stop"
$bin = "C:\Program Files\PostgreSQL\15\bin"
$backups = Join-Path $PSScriptRoot "..\backups"
$latest = Get-ChildItem "$backups\kwikklin-*.dump" | Sort-Object Name | Select-Object -Last 1
if (-not $latest) { Write-Error "backups\ mein koi dump nahi mila"; exit 1 }
Write-Host "Restoring: $($latest.Name)"

$scratch = "laundry_restore_drill"
& "$bin\psql.exe" -h localhost -U postgres -c "DROP DATABASE IF EXISTS $scratch"
& "$bin\psql.exe" -h localhost -U postgres -c "CREATE DATABASE $scratch OWNER laundry"

$env:PGPASSWORD = "laundry"
& "$bin\pg_restore.exe" -h localhost -U laundry -d $scratch --no-owner $latest.FullName
if ($LASTEXITCODE -ne 0) { Write-Warning "pg_restore ne warnings di (aksar harmless: extensions)" }

Write-Host "`nRow counts (scratch vs live):"
foreach ($t in @("customers","orders","conversations","payments","tenants","invoices")) {
    $s = (& "$bin\psql.exe" -h localhost -U laundry -d $scratch -t -c "SELECT count(*) FROM $t").Trim()
    $l = (& "$bin\psql.exe" -h localhost -U laundry -d laundry  -t -c "SELECT count(*) FROM $t").Trim()
    $mark = if ($s -eq $l) { "OK " } else { "DIFF" }
    Write-Host ("  {0}  {1,-14} scratch={2}  live={3}" -f $mark, $t, $s, $l)
}

& "$bin\psql.exe" -h localhost -U postgres -c "DROP DATABASE $scratch"
Write-Host "`nDrill complete — scratch DB removed."
