# Deploy (Woodpecker CI → desktop)

There is a single environment: **production**. Every merge to `main` that
passes CI is published there (handoff repo decision D-017).

```
PR ──────────────► ci: pytest · seed PostGIS · contract (blocking) · docker build (dry run) ──► GitHub check ci/woodpecker/pr/ci
merge to main ───► ci ──► deploy: production ──► smoke tests + public health
Deploy (rollback) ► ci ──► deploy: production (that pipeline's commit) ──► smoke tests + public health
```

| Environment | Trigger | Container (network `spaceapps-net`) | Access |
|---|---|---|---|
| production | push to `main`, or a Woodpecker deployment to `production` | `spaceapps-api-prod` (no host port) | Public Cloudflare URL (`spaceapps-official-cloudflared`; see the handoff repo `docs/07`) |

The tunnel targets the hostname `spaceapps-api-staging`, kept as a network
alias of `spaceapps-api-prod`: restarting the tunnel would change the public
URL. The old test backend (`spaceapps-api`, contract v1.1, tunnel
`spaceapps-cloudflared`) is retired.

Nothing ships unless `scripts/contract_check.py` passes (contract v1.1:
`ErrorResponse`, `lat` in `[-90, 90]`, Apollo sites from the database). In CI
it runs against a throwaway PostGIS service loaded with `db/init`. Locally:
start the seed database (`db/README.md`), set `DATABASE_URL` and run
`python scripts/contract_check.py`.

## How the deploy works

The `deploy` step uses the `deploy_ssh_key` secret (events `push` and
`deployment` only). On the server that key can only run
`/srv/spaceapps/services/deploy/deploy-backend.sh production <sha>`, which:

1. Refuses commits not merged into `origin/main`.
2. Builds the image `spaceapps-backend-official:<sha12>`.
3. Starts a candidate (`spaceapps-api-prod-next`) and waits for `/api/v1/health`.
   If it fails, production is untouched.
4. Swaps it in: the previous container is stopped and kept as
   `spaceapps-api-prod-prev`, the candidate takes the name and the tunnel alias
   (the public URL is down about a second), and health is checked again
   through the alias. If that fails, the previous container is restored.

The `public-health` step checks the public URL from the `public_api_base`
secret (events `push` and `deployment`). Update it when the tunnel URL changes
(handoff repo `docs/07`). A failure there only warns.

## Server environment

`deploy-backend.sh` passes `/srv/spaceapps/services/backend-official/.env` to
every container it starts (`--env-file`), so a change there applies from the
next deploy on, automatic ones included. The file lives only on the server and
is never committed: it holds the database credentials.

| Variable | On the server | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://spaceapps:<server-password>@spaceapps-postgres:5432/spaceapps` | PostGIS on `spaceapps-net` |
| `DATA_DIR` | `/data` (`/srv/spaceapps/data` mounted read-only) | NASA datasets |
| `WEB_CONCURRENCY` | `2` (since 2026-11-15) | Uvicorn worker processes; Uvicorn reads it as the default of `--workers` (1 when unset) |

### Why two workers

Each process reads one DEM at a time (`_RASTER_LOCK`), so a single process
renders tiles on about one core. With two workers, production used two cores
(200 %) under real traffic. Measured on 2026-11-15 with 128 uncached tiles, 8
in parallel, against two containers of the same image side by side: 0.93 s
with one worker and 0.78-0.99 s with two (10-20 % more throughput) with the
DEMs already in the page cache. The gain grows when tiles are slow.

Costs:

- **Memory:** every worker keeps its own raster handles, GDAL block cache and
  tile cache (`TILE_CACHE_MAX_MB`): 1.8 GiB at startup instead of 1.2 GiB, and
  about 3.8 GiB in production after some traffic. During a deploy the old and
  the new container run together for a few seconds.
- **Caches are not shared:** a tile may be rendered once per worker, and
  `TILE_RENDER_CONCURRENCY` applies per worker (up to 8 renders at once).
- **Free RAM matters as much:** the DEMs (`ldem_256.tif` 2.8 GB, the 20 m polar
  DEM 2.5 GB) are read through the page cache, which shares the host memory
  with every other container. Raise workers only with memory to spare.

### Changing or reverting it

Back up the file, edit it (deleting the `WEB_CONCURRENCY` line goes back to one
worker) and redeploy the commit already in production:

```bash
cd /srv/spaceapps/services/backend-official
cp -p .env ".env.bak-$(date -u +%Y%m%dT%H%M%SZ)"
"${EDITOR:-vi}" .env
SHA=$(docker inspect -f '{{index .Config.Labels "spaceapps.commit"}}' spaceapps-api-prod)
sudo -u hackaton /srv/spaceapps/services/deploy/deploy-backend.sh production "$SHA"
docker logs spaceapps-api-prod 2>&1 | grep -c "Started server process"   # workers
```

Run the script as `hackaton`, the deploy key's user: the clone in `src/`
belongs to it. The backup taken before the change of 2026-11-15 is
`.env.bak-20261115T165022Z`.

## Rollback

- **From Woodpecker:** open the pipeline of an older `main` commit → **Deploy**
  → target `production`. It must pass the contract gate too. Requires the
  repository setting **Allow deployments** (`allow_deploy: true`); Woodpecker
  2.8.2 activates repositories with it off and deployments then fail with
  `403 repo does not allow deployments`.
- **By hand on the server**, back to the container kept by the last deploy:

```bash
docker rm -f spaceapps-api-prod \
  && docker rename spaceapps-api-prod-prev spaceapps-api-prod \
  && docker network disconnect spaceapps-net spaceapps-api-prod \
  && docker network connect --alias spaceapps-api-staging spaceapps-net spaceapps-api-prod \
  && docker start spaceapps-api-prod
```

Only one previous container is kept. The one kept by the first deploy under
this scheme was still named `spaceapps-api-staging`; the alias step above
covers both.
