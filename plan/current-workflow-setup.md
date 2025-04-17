# Atlan's Current Workflow Setup

The workflows are a series of containers written by the engineers in Atlan that connect to a data source, extract all the metadata in a batch ETL fashion.

This metadata is transformed and then published to our internal metadata store which is an internal fork of Apache Atlas.

These workflows are usually long running and our customers don't like it when they are long running.

They are long running for multiple reasons:
- Either extract is slow or publish is slow
- When extract is slow, it usually is because of some problems or nuances on the source itself, usually not in the control of Atlan engineers. In these cases we work with our customers to resolve issues.
- When publish is slow, it's mostly because of problems with the metastore. Atlan engineers can do a lot here without bothering the customer because we fully control the metastore and the publish mechanism to publish metadata to the metastore.

Our metastore is not fast at data ingestion, thus we have a diff mechanism to figure out what has changed from the previous run and then just publish the difference.