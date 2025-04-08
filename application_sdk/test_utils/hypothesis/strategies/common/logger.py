"""Hypothesis strategies for testing logger components."""

from unittest import mock

from hypothesis import strategies as st
from hypothesis.strategies import DrawFn


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
        attempt=draw(st.integers()),
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
        attempt=draw(st.integers()),
        schedule_to_close_timeout=f"{draw(st.integers())}s",
        start_to_close_timeout=f"{draw(st.integers())}s",
    )
