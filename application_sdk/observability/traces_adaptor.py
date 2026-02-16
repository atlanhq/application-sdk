"""Traces adaptor; uses light implementation when APPLICATION_MODE=SERVER to save memory."""

from application_sdk.constants import APPLICATION_MODE, ApplicationMode

if APPLICATION_MODE == ApplicationMode.SERVER:
    from application_sdk.observability.traces_adaptor_light import get_traces
else:
    from application_sdk.observability.traces_adaptor_full import get_traces
