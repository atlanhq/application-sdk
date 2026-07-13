-- Canonical hermetic seed for the full-DAG e2e harness (MySQL dialect).
-- Shared by every MySQL-family connector (mysql, mariadb) — DO NOT make this
-- connector-specific. The e2e extracts metadata *shape*, so one canonical
-- dataset per engine gives every connector deterministic, uniform asset counts.
--
-- Shape (MySQL treats each database as a schema):
--   e2e_main      → customers, orders (+ view v_customer_order_totals)
--   e2e_other     → products, inventory
--   e2e_excluded  → legacy_logs
-- Under include_filter=^e2e_main$: 1 database, 2 tables, 1 view, 10 columns.

CREATE DATABASE IF NOT EXISTS e2e_other CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS e2e_excluded CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT SELECT ON e2e_main.* TO 'e2e_user'@'%';
GRANT SELECT ON e2e_other.* TO 'e2e_user'@'%';
GRANT SELECT ON e2e_excluded.* TO 'e2e_user'@'%';
FLUSH PRIVILEGES;

USE e2e_main;

CREATE TABLE customers (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE orders (
    id INT PRIMARY KEY AUTO_INCREMENT,
    customer_id INT NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE OR REPLACE VIEW v_customer_order_totals AS
SELECT c.id AS customer_id, c.name, COALESCE(SUM(o.amount), 0) AS total_spend
FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.name;

INSERT INTO customers (name, email) VALUES
    ('Alice', 'alice@example.com'),
    ('Bob',   'bob@example.com'),
    ('Carol', 'carol@example.com');

INSERT INTO orders (customer_id, amount) VALUES
    (1, 19.95), (1, 42.00), (2, 7.50), (3, 199.00);

USE e2e_other;

CREATE TABLE products (
    id INT PRIMARY KEY AUTO_INCREMENT,
    sku VARCHAR(64) UNIQUE NOT NULL,
    description TEXT
);

CREATE TABLE inventory (
    product_id INT NOT NULL,
    warehouse VARCHAR(32) NOT NULL,
    on_hand INT NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, warehouse),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

INSERT INTO products (sku, description) VALUES
    ('SKU-001', 'Widget'),
    ('SKU-002', 'Gadget');

INSERT INTO inventory (product_id, warehouse, on_hand) VALUES
    (1, 'east', 100), (1, 'west', 50), (2, 'east', 25);

USE e2e_excluded;

CREATE TABLE legacy_logs (
    id INT PRIMARY KEY AUTO_INCREMENT,
    message VARCHAR(500),
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO legacy_logs (message) VALUES ('seed'), ('exclude-me');
