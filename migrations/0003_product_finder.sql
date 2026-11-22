-- Topic 2 (cheapest product finder): every verified offer observed, so price
-- history per product/retailer accumulates over time (and serves as a cache).

CREATE TABLE IF NOT EXISTS product_offers (
    id               BIGINT        NOT NULL AUTO_INCREMENT,
    request_id       VARCHAR(128)  NOT NULL,
    query_key        VARCHAR(255)  NOT NULL,          -- normalised product query
    country_code     VARCHAR(8)    NOT NULL,
    retailer         VARCHAR(255)  NOT NULL,          -- domain
    title            TEXT          NOT NULL,
    url              TEXT          NOT NULL,
    price            DECIMAL(14,2) NOT NULL,
    currency         VARCHAR(8)    NOT NULL,
    shipping         DECIMAL(14,2) NULL,
    total_in_target  DECIMAL(14,2) NULL,
    target_currency  VARCHAR(8)    NULL,
    `condition`      VARCHAR(32)   NULL,
    in_stock         BOOLEAN       NULL,
    match_verdict    VARCHAR(16)   NULL,              -- exact|variant|accessory|mismatch
    observed_at      DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_product_offers_query (query_key, country_code, observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
