"""E001–E011 and E014 — silent exception swallowing rules, one module per rule.

Each ``_eNNN`` module holds one rule's mixin: its ``_check_pNNN`` entry point
and the exemptions that rule owns. :class:`SilentSwallowMixin` composes them,
and :class:`~.._checker.Checker` composes that with the other E-series mixins.

The rule mixins share one ``self``, so a rule may call an exemption another rule
owns (E004 and E007 share their typed-result and failure-record exemptions, E009
reuses E004's stash-for-re-raise test). That call is resolved through the MRO at
runtime; the owning module is the one that defines the method.
"""

from __future__ import annotations

from ._e001_e002_e006 import E001E002E006Mixin
from ._e003 import E003Mixin
from ._e004 import E004Mixin
from ._e005 import E005Mixin
from ._e007 import E007Mixin
from ._e008 import E008Mixin
from ._e009 import E009Mixin
from ._e010 import E010Mixin
from ._e011 import E011Mixin
from ._e014 import E014Mixin

__all__ = ["SilentSwallowMixin"]


class SilentSwallowMixin(
    E001E002E006Mixin,
    E003Mixin,
    E004Mixin,
    E005Mixin,
    E007Mixin,
    E008Mixin,
    E009Mixin,
    E010Mixin,
    E011Mixin,
    E014Mixin,
):
    """Rule methods for E001–E011 and E014 (silent-swallow category)."""
