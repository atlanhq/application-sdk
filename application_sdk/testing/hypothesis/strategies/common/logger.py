"""Hypothesis strategies for testing logger components.

Value ranges (bounded since FND-4292; before that every integer was unbounded):

* ``attempt``: 1 to 2**31 - 1. Temporal counts attempts from 1.
* ``schedule_to_close_timeout`` / ``start_to_close_timeout``: ``"<n>s"`` with
  ``n`` from 0 to 86,400 (one day).
"""

from unittest import mock

from hypothesis import strategies as st
from hypothesis.strategies import DrawFn

#: Temporal attempts start at 1; the bound keeps them a plausible int32.
_attempts = st.integers(min_value=1, max_value=2**31 - 1)

#: Activity timeouts, in whole seconds: up to one day.
_timeout_seconds = st.integers(min_value=0, max_value=86_400)


@st.composite
def workflow_info_strategy(draw: DrawFn) -> mock.Mock:
    """Strategy for generating workflow info test data.

    Args:
        draw: Hypothesis draw function for sampling values.

    Returns:
        A mock object with workflow information fields populated with random data.
    """
    return mock.Mock(
        workflow_id=draw(st.text()),
        run_id=draw(st.text()),
        workflow_type=draw(st.text()),
        namespace=draw(st.text()),
        task_queue=draw(st.text()),
        attempt=draw(_attempts),
    )


@st.composite
def activity_info_strategy(draw: DrawFn) -> mock.Mock:
    """Strategy for generating activity info test data.

    Args:
        draw: Hypothesis draw function for sampling values.

    Returns:
        A mock object with activity information fields populated with random data.
    """
    return mock.Mock(
        workflow_id=draw(st.text()),
        workflow_run_id=draw(st.text()),
        activity_id=draw(st.text()),
        activity_type=draw(st.text()),
        task_queue=draw(st.text()),
        attempt=draw(_attempts),
        schedule_to_close_timeout=f"{draw(_timeout_seconds)}s",
        start_to_close_timeout=f"{draw(_timeout_seconds)}s",
    )
