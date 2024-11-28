import json
import re


def process_text(text: str, max_length: int = 100000) -> str:
    if len(text) > max_length:
        text = text[:max_length]

    text = re.sub(r"<[^>]+>", "", text)

    text = json.dumps(text)

    return text
