-- Topic 1 (EV helper): crawled-fact caches + run audit log.

CREATE TABLE IF NOT EXISTS country_level_info (
    id            BIGINT        NOT NULL AUTO_INCREMENT,
    country_code  VARCHAR(8)    NOT NULL,
    metric        VARCHAR(64)   NOT NULL,
    value         DECIMAL(18,4) NOT NULL,
    currency      VARCHAR(8)    NOT NULL,
    source_url    TEXT          NULL,
    fetched_at    DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_country_metric (country_code, metric)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ev_model_specs (
    id                BIGINT        NOT NULL AUTO_INCREMENT,
    make              VARCHAR(100)  NOT NULL,
    model             VARCHAR(150)  NOT NULL,
    year              INT           NOT NULL,
    `trim`            VARCHAR(150)  NOT NULL DEFAULT '',
    battery_kwh       DECIMAL(12,4) NULL,
    range_km          DECIMAL(12,4) NULL,
    max_charging_kw   DECIMAL(12,4) NULL,
    efficiency_wh_km  DECIMAL(12,4) NULL,
    base_price        DECIMAL(18,4) NULL,
    price_currency    VARCHAR(8)    NULL,
    image_url         TEXT          NULL,
    source_url        TEXT          NULL,
    fetched_at        DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ev_model (make, model, year, `trim`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ev_research_runs (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    request_id        VARCHAR(128) NULL,
    user_id           CHAR(36)     NOT NULL,
    session_id        VARCHAR(64)  NOT NULL,
    query_text        TEXT         NOT NULL,
    parsed_model      VARCHAR(255) NULL,
    parsed_year       INT          NULL,
    specs_source_url  TEXT         NULL,
    tco_inputs        JSON         NULL,
    tco_result        JSON         NULL,
    final_output      MEDIUMTEXT   NULL,
    created_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_ev_research_runs_user (user_id),
    CONSTRAINT fk_ev_research_runs_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
