# Deploy (Woodpecker CI → desktop)

```
PR ──────────────► ci: pytest · contract (report only) · docker build (dry run) ──► GitHub check ci/woodpecker/pr/ci
merge to main ───► ci ──► deploy: staging ──► smoke tests
Deploy/promote ──► ci ──► contract gate (blocking) ──► deploy: production ──► smoke tests + public health
```

| Environment | Trigger | Container (network `spaceapps-net`) | Access |
|---|---|---|---|
| staging | push to `main` | `spaceapps-api-staging` | Internal only (`http://spaceapps-api-staging:8000/api/v1`) |
| production | Woodpecker deployment, target `production` | `spaceapps-api` (host port 8000) | Public Cloudflare URL (unchanged; see the handoff repo `docs/07`) |

## Promote to production

- Woodpecker UI → repository → pipeline of the `main` commit → **Deploy** → target `production`.
- Or via the API (server v2.8.2): `curl -X POST -H "Authorization: Bearer $WOODPECKER_TOKEN" "$WOODPECKER_SERVER/api/repos/<repo-id>/pipelines/<number>?event=deployment&deploy_to=production"`

Production only ships when `scripts/contract_check.py` passes (contract v1.1:
`ErrorResponse`, `lat` in `[-90, 90]`, Apollo sites). Run it locally with
`python scripts/contract_check.py`.

## How the deploy works

The `deploy` step uses the `deploy_ssh_key` secret (events `push` and
`deployment` only). On the server that key can only run
`/srv/spaceapps/services/deploy/deploy-backend.sh <staging|production> <sha>`, which:

1. Refuses commits not merged into `origin/main`.
2. Builds the image `spaceapps-backend-official:<sha12>`.
3. Starts a candidate and waits for `/api/v1/health`.
4. Swaps it in. In production the previous container is kept as
   `spaceapps-api-prev` and restored automatically if the new one fails.

Database credentials live only on the server (`/srv/spaceapps/services/backend-official/.env`).

## Rollback by hand

Promote an older `main` pipeline to `production`, or on the server:

```bash
docker rm -f spaceapps-api && docker rename spaceapps-api-prev spaceapps-api && docker start spaceapps-api
```
