"""Myriaxon: an advanced agentic harness for the terminal and the browser.

## Why `__version__` is defined here and not merely imported

`VERSION` has always lived in `constants`, and `from myriaxon import VERSION` has always worked. What did **not**
work, measured against the built wheel in a clean virtualenv, was the conventional spelling:

    >>> import myriaxon; myriaxon.__version__
    AttributeError

`__version__` is the dunder that packaging tools, bug-report templates and `importlib.metadata` fallbacks actually
probe. A package whose version is only reachable under a non-standard name is a package whose version nobody can
read programmatically, and the failure is silent -- `getattr(module, "__version__", "unknown")` is the idiom, so the
caller degrades to "unknown" instead of raising.

Aliased rather than duplicated: `VERSION` stays the one source of truth so the two cannot drift, and both are
exported because both spellings are in use across this repository.
"""

from .constants import APP_NAME, VERSION

# The conventional spelling. Aliased to VERSION, never assigned a literal.
__version__ = VERSION

__all__ = ["APP_NAME", "VERSION", "__version__"]
