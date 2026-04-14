from hypothesis import strategies as st

# Strategy for generating safe file path components
safe_path_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=20,
).map(lambda x: f"/data/{x}")

# Strategy for generating file names
file_name_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=10,
).map(lambda x: f"{x}.json")

# Strategy for generating lists of file names
file_names_strategy = st.lists(file_name_strategy, unique=True)

# Strategy for generating download file prefixes
download_prefix_strategy = safe_path_strategy

# Strategy for generating complete JsonInput configurations
json_input_config_strategy = st.fixed_dictionaries(
    {
        "path": safe_path_strategy,
        "file_names": st.one_of(st.none(), file_names_strategy),
    }
)
