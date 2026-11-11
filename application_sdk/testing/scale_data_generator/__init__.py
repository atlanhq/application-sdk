"""Deprecated: the retired scale data generator.

No app and no CI job uses the generator, so it leaves the package. The
implementation lives in :mod:`application_sdk.testing._scale_data_generator`;
every name here still resolves to the same object, with a ``DeprecationWarning``
on access.

.. deprecated:: 3.56
    Use faker directly in your own fixture script instead. Will be removed in
    v4.0.0 along with the ``[scale_data_generator]`` extra (FND-4660).
"""
