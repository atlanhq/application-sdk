from hypothesis import strategies as st
from typing import Dict, Any

@st.composite
def temporal_connection_params(draw) -> Dict[str, str]:
    """Generate valid temporal connection parameters."""
    return {
        "host": draw(st.one_of(
            st.just("localhost"),
            st.from_regex(r"[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+").filter(lambda x: len(x) < 255)
        )),
        "port": draw(st.integers(min_value=1, max_value=65535).map(str)),
        "application_name": draw(st.text()),
        "namespace": draw(st.text())
    }

@st.composite
def workflow_credentials(draw) -> Dict[str, str]:
    """Generate workflow credentials."""
    return {
        "username": draw(st.text()),
        "password": draw(st.text())
    }

@st.composite
def workflow_args(draw, include_workflow_id: bool = False) -> Dict[str, Any]:
    """Generate workflow arguments."""
    args = {
        "param1": draw(st.text()),
        "credentials": draw(workflow_credentials())
    }
    
    if include_workflow_id:
        args["workflow_id"] = draw(workflow_id())
    
    return args

@st.composite
def workflow_id(draw) -> str:
    """Generate valid workflow IDs."""
    return draw(st.text())

@st.composite
def run_id(draw) -> str:
    """Generate valid run IDs."""
    return draw(st.text()) 