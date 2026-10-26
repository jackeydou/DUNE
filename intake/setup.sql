-- Run once on a new Neon database, as its owner, before `mise run intake:migrate`. Replace the two
-- passwords. `intake_owner` runs migrations; `intake_app` is the role Hyperdrive connects as and
-- can only read and write the intake schema's rows.
CREATE ROLE intake_owner LOGIN PASSWORD 'change-me-owner';
CREATE ROLE intake_app LOGIN PASSWORD 'change-me-app';
CREATE SCHEMA intake AUTHORIZATION intake_owner;
GRANT USAGE ON SCHEMA intake TO intake_app;
ALTER DEFAULT PRIVILEGES FOR ROLE intake_owner IN SCHEMA intake
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO intake_app;
ALTER DEFAULT PRIVILEGES FOR ROLE intake_owner IN SCHEMA intake
    GRANT USAGE ON SEQUENCES TO intake_app;
ALTER ROLE intake_app SET search_path = intake;
