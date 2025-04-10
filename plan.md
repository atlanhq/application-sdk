## Guidelines

The following is a plan to implement a diff calculator on a set of JSON records, this is to be implemented in the `application_sdk` package.

The whole implementation is supposed to be in a streaming format, we're using daft so the dataframes are lazily loaded, so everything should accept an Iterator of daft.Dataframe, and return an Iterator of daft.Dataframe.

## Data Loading

- Implement functionality to load data from a prefix of JSON files into an iterator of daft dataframes
- This is the columns in which you must load the
	- typeName -- pick this from the `typeName` key in the record
	- qualifiedName -- pick this from the `attributes.qualifiedName` key in the record
	- record -- stringified JSON with keys sorted, use orjson for this

## Hashing

Use a daft UDF like the following to create the hash of a given colum

```python
@daft.udf(return_dtype=daft.DataType.string())
def hash(col: daft.Series) -> List[str]:
	return [
		xxh3_128(orjson.dumps(x, option=orjson.OPT_SORT_KEYS)).hexdigest()
		for x in col
	]
```

- Columns that needed to be added from the data frame output of the data loading step
	- typeName
	- qualifiedName
	- record
	- attributesHash -- key is `attributes` in the record
	- customAttributesHash -- key is `customAttributes` in the record
	- businessAttributesHash -- key is `businessAttributes` in the record
	- relationshipAttributesHash -- key is `relationshipAttributes` in the record
	- classificationsHash -- key is `classifications` in the record
	- termsHash -- key is `terms` in the record
	- fullHash

## Partitioning

Add a column to the output from the Hashing dataframe call partition_key

In the current diff implementation, processing records requires having the current record, previous record all within the same worker context.

This creates a bottleneck for parallelization since we need to ensure related records stay together.

We'll implement a partitioning strategy based on consistent hashing to ensure that records with the same qualified name (or qualified name + type combination) always land in the same partition, enabling efficient parallel processing.

Let's walk through a concrete example of how this would work.

For illustration sake, let’s assume we have

- 4 partitions
- A collection of records with qualified names

### Sample Hash Function

First, we define a consistent hashing function that maps a qualified name to a partition:

```python
def get_partition(qualified_name: str, type_name: str, num_partitions=4):
    # Combine qualified name and type if type is provided
    key = f"{type_name}/{qualified_name}"
    hash_value = xxh3_128(key)

    return abs(hash_value) % num_partitions
```

Let's consider a set of asset records in different states (previous, current):

|Qualified Name|Type|Version|Data|
|---|---|---|---|
|default/snowflake/123/customer.1001|Person|previous|{ name: "John Doe", email: "[john@example.com](mailto:john@example.com)" }|
|default/snowflake/123/customer.1001|Person|current|{ name: "John Doe", email: "[johndoe@example.com](mailto:johndoe@example.com)" }|
|default/snowflake/123/customer.1002|Person|previous|{ name: "Jane Smith", phone: "555-1234" }|
|default/snowflake/123/customer.1002|Person|current|{ name: "Jane Smith", phone: "555-5678" }|
|default/snowflake/123/product.5001|Item|previous|{ title: "Laptop", price: 999 }|
|default/snowflake/123/product.5001|Item|current|{ title: "Laptop", price: 899, onSale: true }|
|default/snowflake/123/customer.1003|Company|previous|{ name: "Acme Inc", sector: "Technology" }|
|default/snowflake/123/customer.1003|Company|current|{ name: "Acme Inc", sector: "Information Technology" }|

Let's apply our partitioning strategy:

```python
// Example partition assignments (based on our hashing function)
get_partition("default/snowflake/123/customer.1001", "Person", 4)  → 2
get_partition("default/snowflake/123/customer.1002", "Person", 4)  → 1
get_partition("default/snowflake/123/product.5001", "Item", 4)     → 3
get_partition("default/snowflake/123/customer.1003", "Company", 4) → 0
```

This would result in the following distribution:

**Partition 0:**

- customer.1003 (Company) - previous
- customer.1003 (Company) - current

**Partition 1:**

- customer.1002 (Person) - previous
- customer.1002 (Person) - current

**Partition 2:**

- customer.1001 (Person) - previous
- customer.1001 (Person) - current

**Partition 3:**

- product.5001 (Item) - previous
- product.5001 (Item) - current

The input to this step is the output dataframe from the hash step

Use above sepcification to add a column `partition` to the input dataframe and return that as the output dataframe


## Diff Calculation

Just create an enum `DiffState` with the following values

- `NEW`
- `DIFF`
- `NO_DIFF`
- `DELETE`

Take the dataframe from the
## Output

Take the dataframe from the diff calculation step and add write a function to export it with takes a key to indicate format.

The key can have values `json` and `parquet`

Write it to a specified prefix in the format specified by the key, but also add an implementation do hive partitioning
