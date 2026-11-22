-- Local MySQL only (mounted into /docker-entrypoint-initdb.d, runs once on an empty volume):
-- a disposable database for the MySQL integration test (README "Database"), so integration tests never touch dev data.
CREATE DATABASE IF NOT EXISTS agency_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON agency_test.* TO 'agency'@'%';
