"""Logger adaptor; uses light implementation when APPLICATION_MODE=SERVER to save memory."""

from application_sdk.constants import APPLICATION_MODE, ApplicationMode

if APPLICATION_MODE == ApplicationMode.SERVER:
    from application_sdk.observability.logger_adaptor_light import (
        default_logger,
        get_logger,
    )
else:
    from application_sdk.observability.logger_adaptor_full import (
        default_logger,
        get_logger,
    )
