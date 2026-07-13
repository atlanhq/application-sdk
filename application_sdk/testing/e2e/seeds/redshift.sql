-- Canonical hermetic seed for the full-DAG e2e harness (Amazon Redshift dialect).
-- Rung 3 (DataForge): Redshift has no local container, so this DDL is applied to
-- a live instance provisioned via the DataForge API (see dataforge.py). Byte-shape
-- mirrors the postgres seed so asset counts stay uniform across engines.
--
-- Applied against the DataForge-provisioned database (e2e_main). Redshift nests
-- schemas under one database, so the canonical shape uses three schemas:
--   e2e_main      -> customers, orders (+ view v_customer_order_totals)
--   e2e_other     -> products, inventory
--   e2e_excluded  -> legacy_logs
-- Under include_filter scoped to schema e2e_main: 1 database, 1 schema,
-- 2 tables, 1 view, 10 columns.
--
-- Redshift dialect notes: SERIAL -> INTEGER IDENTITY(1,1); TEXT -> VARCHAR;
-- FK/PK/UNIQUE are accepted but informational (not enforced) on Redshift; the
-- view is created WITH NO SCHEMA BINDING to avoid late-binding drop coupling.

CREATE SCHEMA IF NOT EXISTS e2e_main;
CREATE SCHEMA IF NOT EXISTS e2e_other;
CREATE SCHEMA IF NOT EXISTS e2e_excluded;

CREATE TABLE e2e_main.customers (
    id INTEGER IDENTITY(1,1) PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(200),
    created_at TIMESTAMP DEFAULT SYSDATE
);

CREATE TABLE e2e_main.orders (
    id INTEGER IDENTITY(1,1) PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES e2e_main.customers (id),
    amount DECIMAL(10, 2) NOT NULL,
    placed_at TIMESTAMP DEFAULT SYSDATE
);

CREATE OR REPLACE VIEW e2e_main.v_customer_order_totals AS
SELECT c.id AS customer_id, c.name, COALESCE(SUM(o.amount), 0) AS total_spend
FROM e2e_main.customers c
LEFT JOIN e2e_main.orders o ON o.customer_id = c.id
GROUP BY c.id, c.name
WITH NO SCHEMA BINDING;

INSERT INTO e2e_main.customers (name, email) VALUES
    ('Alice', 'alice@example.com'),
    ('Bob',   'bob@example.com'),
    ('Carol', 'carol@example.com');

INSERT INTO e2e_main.orders (customer_id, amount) VALUES
    (1, 19.95), (1, 42.00), (2, 7.50), (3, 199.00);

CREATE TABLE e2e_other.products (
    id INTEGER IDENTITY(1,1) PRIMARY KEY,
    sku VARCHAR(64) UNIQUE NOT NULL,
    description VARCHAR(500)
);

CREATE TABLE e2e_other.inventory (
    product_id INTEGER NOT NULL REFERENCES e2e_other.products (id),
    warehouse VARCHAR(32) NOT NULL,
    on_hand INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, warehouse)
);

INSERT INTO e2e_other.products (sku, description) VALUES
    ('SKU-001', 'Widget'),
    ('SKU-002', 'Gadget');

INSERT INTO e2e_other.inventory (product_id, warehouse, on_hand) VALUES
    (1, 'east', 100), (1, 'west', 50), (2, 'east', 25);

CREATE TABLE e2e_excluded.legacy_logs (
    id INTEGER IDENTITY(1,1) PRIMARY KEY,
    message VARCHAR(500),
    logged_at TIMESTAMP DEFAULT SYSDATE
);

INSERT INTO e2e_excluded.legacy_logs (message) VALUES ('seed'), ('exclude-me');
