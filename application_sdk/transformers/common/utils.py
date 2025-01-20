import json
import re


def process_text(text: str, max_length: int = 100000) -> str:
    if len(text) > max_length:
        text = text[:max_length]

    text = re.sub(r"<[^>]+>", "", text)

    text = json.dumps(text)

    return text


def build_phoenix_uri(connector_name: str, connector_type: str, *args: str) -> str:
    """
    Build the URI for a Phoenix entity.
    """
    return f"/{connector_name}/{connector_type}/{'/'.join(args)}"


def build_atlas_qualified_name(connection_qualified_name: str, *args: str) -> str:
    """
    Build the qualified name for an Atlas entity.
    """
    return f"{connection_qualified_name}/{'/'.join(args)}"
