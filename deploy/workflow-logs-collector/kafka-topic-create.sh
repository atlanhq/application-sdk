#!/bin/bash
# Create the workflow-logs Kafka topic
# Run from any pod with kubectl access or exec into kafka-0
#
# Usage:
#   kubectl exec -it kafka-0 -n kafka -- bash /path/to/kafka-topic-create.sh
#   OR copy-paste the kafka-topics.sh command below

set -euo pipefail

BOOTSTRAP_SERVER="kafka-headless.kafka.svc.cluster.local:9092"
TOPIC_NAME="workflow-logs"

echo "Creating Kafka topic: ${TOPIC_NAME}"

kafka-topics.sh \
  --create \
  --bootstrap-server "${BOOTSTRAP_SERVER}" \
  --topic "${TOPIC_NAME}" \
  --partitions 6 \
  --replication-factor 3 \
  --config retention.ms=3600000 \
  --config retention.bytes=-1 \
  --config segment.ms=300000 \
  --config cleanup.policy=delete \
  --config max.message.bytes=1048576 \
  --config compression.type=lz4

echo "Verifying topic creation..."
kafka-topics.sh \
  --describe \
  --topic "${TOPIC_NAME}" \
  --bootstrap-server "${BOOTSTRAP_SERVER}"

echo "Done. Topic '${TOPIC_NAME}' created successfully."
