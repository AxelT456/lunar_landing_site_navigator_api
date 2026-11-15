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

Database credentials live only on the server (`/srv/spaceapps/services/backend-official/.env`).

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
