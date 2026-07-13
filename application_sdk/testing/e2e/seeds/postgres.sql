-- Canonical hermetic seed for the full-DAG e2e harness (PostgreSQL dialect).
-- Shared by every Postgres-family connector (postgres, cloudsqlpostgres,
-- alloydbpostgres) — the e2e extracts metadata *shape*, so one canonical
-- dataset per engine gives deterministic, uniform asset counts.
--
-- Runs (via /docker-entrypoint-initdb.d) as POSTGRES_USER inside POSTGRES_DB
-- (e2e_main). Postgres nests schemas under one database, so the canonical shape
-- uses three schemas rather than three databases:
--   e2e_main      → customers, orders (+ view v_customer_order_totals)
--   e2e_other     → products, inventory
--   e2e_excluded  → legacy_logs
-- Under include_filter scoped to schema e2e_main: 1 database, 1 schema,
-- 2 tables, 1 view, 10 columns.

CREATE SCHEMA IF NOT EXISTS e2e_main;
CREATE SCHEMA IF NOT EXISTS e2e_other;
CREATE SCHEMA IF NOT EXISTS e2e_excluded;

CREATE TABLE e2e_main.customers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE e2e_main.orders (
    id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES e2e_main.customers (id),
    amount NUMERIC(10, 2) NOT NULL,
    placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE VIEW e2e_main.v_customer_order_totals AS
SELECT c.id AS customer_id, c.name, COALESCE(SUM(o.amount), 0) AS total_spend
FROM e2e_main.customers c
LEFT JOIN e2e_main.orders o ON o.customer_id = c.id
GROUP BY c.id, c.name;

INSERT INTO e2e_main.customers (name, email) VALUES
    ('Alice', 'alice@example.com'),
    ('Bob',   'bob@example.com'),
    ('Carol', 'carol@example.com');

INSERT INTO e2e_main.orders (customer_id, amount) VALUES
    (1, 19.95), (1, 42.00), (2, 7.50), (3, 199.00);

CREATE TABLE e2e_other.products (
    id SERIAL PRIMARY KEY,
    sku VARCHAR(64) UNIQUE NOT NULL,
    description TEXT
);

CREATE TABLE e2e_other.inventory (
    product_id INT NOT NULL REFERENCES e2e_other.products (id),
    warehouse VARCHAR(32) NOT NULL,
    on_hand INT NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, warehouse)
);

INSERT INTO e2e_other.products (sku, description) VALUES
    ('SKU-001', 'Widget'),
    ('SKU-002', 'Gadget');

INSERT INTO e2e_other.inventory (product_id, warehouse, on_hand) VALUES
    (1, 'east', 100), (1, 'west', 50), (2, 'east', 25);

CREATE TABLE e2e_excluded.legacy_logs (
    id SERIAL PRIMARY KEY,
    message VARCHAR(500),
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO e2e_excluded.legacy_logs (message) VALUES ('seed'), ('exclude-me');
