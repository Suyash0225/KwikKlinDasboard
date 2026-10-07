# Deploy — AWS (Ubuntu EC2)

Ek machine par sab: Postgres + FastAPI (uvicorn, systemd) + nginx (HTTPS,
certbot). Domain `kwikklin.online` EC2 ke public IP par point karta hai.

## Pehli baar (ek baar ka kaam, ~30 min)

```bash
# 1. system packages
sudo apt update && sudo apt install -y python3.12 python3.12-venv git nginx postgresql postgresql-client certbot python3-certbot-nginx

# 2. database
sudo -u postgres psql -c "CREATE USER laundry WITH PASSWORD '<strong-password>';"
sudo -u postgres psql -c "CREATE DATABASE laundry OWNER laundry;"

# 3. code
sudo mkdir -p /opt/kwikklin && sudo chown $USER /opt/kwikklin
git clone https://github.com/Suyash0225/KwikKlinDasboard.git /opt/kwikklin
cd /opt/kwikklin
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 4. .env — laptop wali .env scp se laao, ya .env.example se banao
scp .env ubuntu@<server-ip>:/opt/kwikklin/.env      # (laptop se)
nano .env
```

`.env` mein server ke liye ye pakka badlo:

| Key | Value |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://laundry:<password>@localhost:5432/laundry` |
| `ENVIRONMENT` | `production` |
| `SITE_URL` | `https://kwikklin.online` |
| `APP_BASE_URL` | `https://kwikklin.online` |
| `RATE_LIMIT_PER_MIN` | jaisa hai |

WhatsApp/Razorpay/Google/Gemini keys laptop wali hi chalengi.

```bash
# 5. DB schema + home dukaan + keys
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.bootstrap_home_tenant
.venv/bin/python -m scripts.secure_setup        # TOKEN_ENCRYPTION_KEY, VENDOR_API_KEY, kk_app role -> .env
.venv/bin/python -m scripts.seed_rates          # rate card (ya dashboard se)
```

**Ab `.env` ka backup lo** (password manager mein). `TOKEN_ENCRYPTION_KEY`
kho gayi to DB ke saare WhatsApp/Instagram/Google token bekaar.

### systemd service

`/etc/systemd/system/kwikklin.service`:

```ini
[Unit]
Description=Kwik Klin
After=network.target postgresql.service

[Service]
User=ubuntu
WorkingDirectory=/opt/kwikklin
EnvironmentFile=/opt/kwikklin/.env
# 1 worker: scheduler app ke andar chalta hai; kai workers = kai scheduler.
# Load badhe to workers badhao aur scheduler ek alag process mein le jao.
ExecStart=/opt/kwikklin/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips="*"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kwikklin
curl -s http://127.0.0.1:8000/health      # {"status":"ok","db":"connected"}
```

### nginx + HTTPS

`/etc/nginx/sites-available/kwikklin`:

```nginx
server {
    server_name kwikklin.online www.kwikklin.online;
    client_max_body_size 20m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300;
        proxy_buffering off;          # dashboard ki live (SSE) updates
    }
}
```

Phone par panel tez rakhne ke liye (ek round trip India se ~0.3-0.9 s hai,
isliye har request bachana matlab seconds bachana):

```nginx
    # certbot ke baad `listen 443 ssl;` mein http2 jodo — ek connection par
    # sab requests, har asset ke liye naya TLS handshake nahi
    listen 443 ssl http2;

    # Static files nginx khud de — app (uvicorn) ko chhoo kar nahi.
    # Path wahi jahan repo hai (deploy.sh wala folder).
    location /admin/static/ {
        alias /home/ec2-user/kwikKlinDasboard/app/static/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        gzip on; gzip_types text/css application/javascript image/svg+xml; gzip_min_length 1024;
    }
    location /site/assets/ {
        alias /home/ec2-user/kwikKlinDasboard/app/site/assets/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        gzip on; gzip_types text/css application/javascript image/svg+xml; gzip_min_length 1024;
    }
```

```bash
sudo ln -s /etc/nginx/sites-available/kwikklin /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d kwikklin.online -d www.kwikklin.online   # HTTPS, auto-renew
```

AWS Security Group: inbound **80, 443** sab ke liye; **22** sirf apne IP se;
5432 kabhi nahi.

### Bahar ki settings (ek baar)

- **Meta (WhatsApp):** webhook URL `https://kwikklin.online/webhook`, verify
  token = `.env` ka `WHATSAPP_VERIFY_TOKEN`; ya `python -m scripts.set_public_url`.
- **Google Cloud:** OAuth redirect URIs
  `https://kwikklin.online/api/auth/google/callback` aur
  `https://kwikklin.online/control/api/google-business/callback`.
- **Razorpay:** webhook `https://kwikklin.online/webhooks/razorpay`, secret =
  `RAZORPAY_WEBHOOK_SECRET`; website verification ke pages `/about`,
  `/contact`, `/privacy`, `/terms`, `/refund-policy`, `/shipping-policy`.
- Control panel login: `.env` ki `VENDOR_API_KEY`.

## Har update par (naya code aaya)

```bash
cd /opt/kwikklin && ./scripts/deploy.sh
```

Ye backup → `git pull` → pip → `alembic upgrade head` → `secure_setup` →
`systemctl restart kwikklin` → `/health` check karta hai. Aakhir mein `READY`
na dikhe to `journalctl -u kwikklin -n 100` dekho.

## Kaam ki commands

```bash
sudo journalctl -u kwikklin -f            # live logs
sudo systemctl status kwikklin
ls -la /opt/kwikklin/backups/             # nightly pg_dump (14 din)
.venv/bin/python -m scripts.encrypt_tokens # purane plaintext token (secure_setup khud chalata hai)
```

## Naya server / disaster

Naye EC2 par "Pehli baar" wala kram, `.env` backup se copy, aur DB
`pg_restore -d laundry backups/kwikklin-<latest>.dump`. Tokens tabhi
khulenge jab `.env` ki `TOKEN_ENCRYPTION_KEY` wahi ho.
