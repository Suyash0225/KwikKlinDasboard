# Kwik Klin bot keepalive — Windows Startup se chalta hai.
# Har 5 minute: server zinda? tunnel zinda? Nahi to khud dobara chalu.
# (Tunnel ka naya URL + Meta webhook sync tunnel-guard khud karta hai.)

$proj = "C:\Users\Suyash\KwikKlinDasboard"
$py = "$proj\.venv\Scripts\python.exe"

while ($true) {
    # 1) uvicorn (bot server)
    $alive = $false
    try {
        $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/health" -TimeoutSec 8
        if ($r.StatusCode -eq 200) { $alive = $true }
    } catch {}
    if (-not $alive) {
        Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
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
