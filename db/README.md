# Local database (PostGIS seed)

Snapshot of the reference data served by the backend running on the `desktop`
server, so everyone can develop offline without Tailscale or access to the server.

| Table | Contents | Rows |
|---|---|---|
| `landing_sites` | Curated sites (10 south pole Artemis III / CLPS + Apollo 11, 15, 17) with LOLA elevation, slope and `nearest_psr_km` | 13 |
| `psr_regions` | NASA LOLA permanently shadowed regions (`MultiPolygon`, SRID 0, south polar stereographic metres) | 1,531 |
| `horizon_profiles` | Horizon cache (schema only, filled at runtime) | 0 |
| `ephemeris_cache` | Ephemeris cache (schema only, filled at runtime) | 0 |

Fixes on top of the seed live in `db/init/20_*.sql`; they run after it on new databases and are idempotent, so they can be applied once to an existing one (`psql "$DATABASE_URL" -f db/init/20_im2_lroc_site.sql`).

Source: NASA LOLA / PGDA (public domain). The dump contains no roles or passwords.

## Usage

Requires Docker (Docker Desktop, OrbStack or Colima).

```bash
docker compose -f docker-compose.dev.yml up -d
cp .env.example .env   # DATABASE_URL=postgresql://spaceapps:spaceapps_local@localhost:5432/spaceapps
```

The first start loads `db/init/*` automatically (a few seconds). Check it:

```bash
docker exec spaceapps-postgres-dev psql -U spaceapps -d spaceapps \
  -c "select count(*) from landing_sites; select count(*) from psr_regions;"
```

If port 5432 is already taken: `DB_PORT=5433 docker compose -f docker-compose.dev.yml up -d`
and adjust `DATABASE_URL`.

### Reloading the seed

The init scripts only run when the volume is empty. To reload after updating
`10_seed.sql.gz`:

```bash
docker compose -f docker-compose.dev.yml down -v
docker compose -f docker-compose.dev.yml up -d
```

## Regenerating the seed (from the server)

```bash
ssh anthony@desktop "docker exec spaceapps-postgres pg_dump -U spaceapps -d spaceapps \
  --no-owner --no-privileges \
  -t public.landing_sites -t public.psr_regions -t public.horizon_profiles -t public.ephemeris_cache \
  --exclude-table-data=public.horizon_profiles --exclude-table-data=public.ephemeris_cache" \
  | gzip -9 > db/init/10_seed.sql.gz
```
