# Kwik Klin bot keepalive — Windows Startup se chalta hai.
# Har 5 minute: server zinda? tunnel zinda? Nahi to khud dobara chalu.
# (Tunnel ka naya URL + Meta webhook sync tunnel-guard khud karta hai.)

$proj = "C:\Users\Suyash\KwikKlinDasboard"
$py = "$proj\.venv\Scripts\python.exe"

while ($true) {
    # 0) Postgres service zinda? (app se pehle — bina DB ke sab bekar)
    $pg = Get-Service postgresql-x64-15 -ErrorAction SilentlyContinue
    if ($pg -and $pg.Status -ne "Running") {
        try { Start-Service postgresql-x64-15 } catch {}
    }

    # 1) uvicorn (bot server)
    # 200 = sab theek. 503 = app zinda hai par DB down — app ko MAT maaro,
    # DB upar aate hi khud recover ho jayega (pool_pre_ping).
    $alive = $false
    try {
        $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/health" -TimeoutSec 8
        if ($r.StatusCode -eq 200) { $alive = $true }
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 503) { $alive = $true }
    }
    if (-not $alive) {
        Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
        # pehle migrations — naye code ke saath purana schema kabhi na chale
        Start-Process -Wait -WindowStyle Hidden -WorkingDirectory $proj -FilePath $py `
            -ArgumentList "-m","alembic","upgrade","head"
        Start-Process -WindowStyle Hidden -WorkingDirectory $proj -FilePath $py `
            -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000"
    }

    # 2) cloudflared (tunnel) — URL/webhook sync server ke andar ka guard karega
    if (-not (Get-Process cloudflared -ErrorAction SilentlyContinue)) {
        Start-Process -WindowStyle Hidden -FilePath "cloudflared" `
            -ArgumentList "tunnel","--url","http://127.0.0.1:8000"
    }

    Start-Sleep -Seconds 300
}
