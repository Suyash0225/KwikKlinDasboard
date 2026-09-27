# EC2 development server

Use the feature branch for website work without touching the production
kwikklin service or production Postgres.

## First-time setup

From the repo directory:

~~~
git checkout feature/control-google-auth
git pull origin feature/control-google-auth

python3 -m venv .venv-dev
.venv-dev/bin/pip install -r requirements.txt

cp .env.dev.example .env.dev

docker compose -f docker-compose.dev.yml up -d
.venv-dev/bin/alembic upgrade head
~~~

The dev database is laundry_dev on 127.0.0.1:5433. It has its own Docker
volume and container laundry-db-dev.

## Start the development website

~~~
cd /home/ec2-user/kwikKlinDasboard
export KWIKKLIN_ENV_FILE=.env.dev
.venv-dev/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
~~~

Keep this terminal open. Python/template changes reload automatically.

Because the server binds to 127.0.0.1, it is NOT directly reachable from the
internet. Use an SSH tunnel from your own computer:

~~~
ssh -L 8001:127.0.0.1:8001 ec2-user@YOUR_EC2_HOST
~~~

Then open http://127.0.0.1:8001

## Daily workflow

~~~
git checkout feature/control-google-auth
git pull origin feature/control-google-auth
export KWIKKLIN_ENV_FILE=.env.dev
docker compose -f docker-compose.dev.yml up -d
.venv-dev/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
~~~

Edit website files under app/site/, save, refresh the browser and test.
No production restart/deploy is needed.

## Important safety rules

- Do NOT use production .env for the dev process.
- Do NOT point .env.dev at the production database.
- Do NOT set WHATSAPP_PROVIDER=waha here.
- Do NOT expose port 8001 publicly.
- Production continues to use its existing kwikklin systemd service and port.
- When the website change is finished, commit/push the branch, review it, merge
  to main, then deploy production normally.

## Stop dev

Press Ctrl+C for Uvicorn. The development Postgres can stay running, or stop
it with:

~~~
docker compose -f docker-compose.dev.yml stop
~~~
