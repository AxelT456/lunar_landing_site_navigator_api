-- PostGIS must exist before 10_seed.sql.gz creates geometry columns.
CREATE EXTENSION IF NOT EXISTS postgis;
