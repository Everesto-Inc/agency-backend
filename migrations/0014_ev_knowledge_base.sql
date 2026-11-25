-- EV knowledge base (ev_research.kb): official prices land in country_level_info with where they come from;
-- the Open EV Data catalogue (specs of ~1,300 EVs) in ev_catalog. The EV Helper reads these before any web search.
ALTER TABLE country_level_info ADD COLUMN source VARCHAR(160) NULL AFTER source_url;
ALTER TABLE country_level_info ADD COLUMN period VARCHAR(32) NULL AFTER source;
ALTER TABLE country_level_info ADD COLUMN official TINYINT(1) NOT NULL DEFAULT 0 AFTER period;

CREATE TABLE IF NOT EXISTS ev_catalog (
    id                     BIGINT        NOT NULL AUTO_INCREMENT,
    source                 VARCHAR(32)   NOT NULL,
    source_id              VARCHAR(64)   NOT NULL,
    make                   VARCHAR(100)  NOT NULL,
    model                  VARCHAR(150)  NOT NULL,
    variant                VARCHAR(150)  NOT NULL DEFAULT '',
    release_year           INT           NULL,
    usable_battery_kwh     DECIMAL(8,2)  NOT NULL,
    consumption_kwh_100km  DECIMAL(8,2)  NULL,
    est_range_km           INT           NULL,
    dc_max_kw              DECIMAL(8,2)  NULL,
    ac_max_kw              DECIMAL(8,2)  NULL,
    charging_curve         JSON          NULL,
    updated_at             DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ev_catalog_source (source, source_id),
    KEY idx_ev_catalog_make (make)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
