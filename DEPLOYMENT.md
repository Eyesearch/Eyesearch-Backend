# Running the backend on the GCP VM

Everything here assumes PostgreSQL is already running on the same VM and
listening on `127.0.0.1:5432`, which is the secure default and all this
deployment needs.

## The crawler switch

The flag is **`CRAWLER_ENABLED`**, not `ENABLE_CRAWLER`. Settings fields are read
from the environment by name, so:

| Variable | Default | Effect when `false` |
|---|---|---|
| `CRAWLER_ENABLED` | `true` | the crawl worker is never started; the API keeps serving |
| `REVALIDATION_ENABLED` | `true` | the re-crawl loop is never started |

Both are checked in `app/indexer/scheduler.py`, which simply does not create the
task. Nothing crawls, the queue stops draining, and `/v1/search` keeps answering
from whatever is already indexed. The startup log prints the effective value, so
you can confirm it without guessing:

    effective crawl settings: enabled=False batch_size=... concurrency=...

To pause only crawling but keep serving, set `CRAWLER_ENABLED=false` in `.env` and
`docker compose up -d` again.

## Deploy

1. `cp .env.example .env` and set `POSTGRES_PASSWORD` and `CORS_ORIGINS`.
2. `docker compose up -d --build`
3. `docker compose logs -f backend`

`DATABASE_URL`, `ENVIRONMENT` and `ALLOW_LOCAL_DB` are set by the compose file
rather than `.env`, so they cannot drift from what the container needs.

Confirm both `/health` and `/ready` report `db: up`.

### Why host networking

`network_mode: host` puts the container in the VM's network namespace, so
`127.0.0.1:5432` inside the container is the VM's PostgreSQL. The alternative is
a bridge network, which means PostgreSQL must also listen on `172.17.0.1` and
`pg_hba.conf` must trust the docker subnet — more moving parts, and it widens
what can reach the database. The commented block at the bottom of
`docker-compose.yml` has the details if you prefer it.

## Starting a fresh index

The old 4,000 rows are being dropped, so start from empty:

    docker compose exec backend python scripts/enqueue_urls.py --reset --yes

That truncates `crawl_queue`, `pages` and `sites` and resets the identity
counters. Add `--dry-run` first if you want to see what it would do.

## Triggering indexing for specific sites

The crawler takes its work from `crawl_queue`, so queueing a URL is the trigger —
no code change and no rebuild:

    docker compose exec backend python scripts/enqueue_urls.py \
        https://karnataka.gov.in/ https://thehindu.com/

Or keep the list in a file, one URL per line, `#` for comments:

    docker compose exec backend python scripts/enqueue_urls.py --file seeds.txt

What to know about scope:

* A queued URL **is fetched regardless of the whitelist.** The whitelist only
  gates *link discovery* — which links a fetched page is allowed to follow.
* So to let a seeded site expand beyond the URL you gave it, its domain must be
  in `WHITELISTED_DOMAINS` (in `app/agents/01_frontier_agent.py`), or you set
  `ALLOW_GLOBAL_DOMAINS=true` and let it roam.
* `MAX_PAGES_PER_DOMAIN` (default 25) caps how many pages one domain contributes.
* The queue is deduplicated on URL, so re-running the command is safe and reports
  how many were already queued.

Watch progress on `/v1/stats`.

## Without Docker: a systemd unit

If you would rather run it directly on the VM:

    # /etc/systemd/system/eyesearch.service
    [Unit]
    Description=EyeSearch backend
    After=network.target postgresql.service

    [Service]
    WorkingDirectory=/opt/eyesearch/Eyesearch-Backend
    EnvironmentFile=/opt/eyesearch/Eyesearch-Backend/.env
    ExecStart=/opt/eyesearch/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
    Restart=always
    RestartSec=5
    User=eyesearch
    # The parser handles untrusted HTML.
    NoNewPrivileges=true
    PrivateTmp=true
    ProtectSystem=strict
    ReadWritePaths=/opt/eyesearch/Eyesearch-Backend

    [Install]
    WantedBy=multi-user.target

    sudo systemctl daemon-reload
    sudo systemctl enable --now eyesearch

## Still to do on your side

* `render.yaml` is still in the repo. Delete it once Render is gone so nothing
  tries to deploy from it again.
* Put nginx or Caddy in front of port 8000 if the API should be reachable from
  outside the VM, and keep PostgreSQL on `127.0.0.1` either way.
* Rotate the database password that was shared in chat.
