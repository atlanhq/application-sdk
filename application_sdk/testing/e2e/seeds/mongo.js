// Canonical hermetic seed for the full-DAG e2e harness (MongoDB).
// Shared by every MongoDB-family connector. Mongo nests collections under
// databases; the crawler extracts databases + collections (+ inferred fields),
// so this mirrors the SQL canonical shape one level down:
//   e2e_main     → customers, orders
//   e2e_other    → products, inventory
//   e2e_excluded → legacy_logs
// Runs via /docker-entrypoint-initdb.d as the root user at container init.

db.getSiblingDB("e2e_main").customers.insertMany([
  { name: "Alice", email: "alice@example.com" },
  { name: "Bob", email: "bob@example.com" },
  { name: "Carol", email: "carol@example.com" },
]);
db.getSiblingDB("e2e_main").orders.insertMany([
  { customer: "Alice", amount: 19.95 },
  { customer: "Alice", amount: 42.0 },
  { customer: "Bob", amount: 7.5 },
  { customer: "Carol", amount: 199.0 },
]);

db.getSiblingDB("e2e_other").products.insertMany([
  { sku: "SKU-001", description: "Widget" },
  { sku: "SKU-002", description: "Gadget" },
]);
db.getSiblingDB("e2e_other").inventory.insertMany([
  { product: "SKU-001", warehouse: "east", on_hand: 100 },
  { product: "SKU-001", warehouse: "west", on_hand: 50 },
  { product: "SKU-002", warehouse: "east", on_hand: 25 },
]);

db.getSiblingDB("e2e_excluded").legacy_logs.insertMany([
  { message: "seed" },
  { message: "exclude-me" },
]);
