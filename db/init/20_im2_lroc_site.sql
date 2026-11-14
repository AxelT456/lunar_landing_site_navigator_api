-- IM-2 Athena: use the LROC target site (-84.78, 29.13 E) instead of lon 31.2,
-- which had no source and sat ~5.7 km away (handoff repo PREGUNTAS.md P-009).
-- Source: https://www.lroc.asu.edu/images/1401 (NASA: Athena landed ~400 m from it).
-- Polar coordinates in the LOLA CRS (D-012); elevation and slope from LOLA 20 m
-- (LDEM/LDSM_80S_20MPP_ADJ), nearest PSR from psr_regions.
--
-- Runs after 10_seed on new databases. Idempotent, so it can also be applied once
-- to an existing database:
--   psql "$DATABASE_URL" -f db/init/20_im2_lroc_site.sql
UPDATE landing_sites
SET lat = -84.78,
    lon = 29.13,
    polar_x_m = 77106.74964669526,
    polar_y_m = 138362.9528972543,
    elevation_m = 5312.558669334091,
    slope_deg = 1.1338159399480994,
    nearest_psr_km = 9.347,
    geom = ST_MakePoint(77106.74964669526, 138362.9528972543)
WHERE slug = 'clps-im2-mons-mouton';
