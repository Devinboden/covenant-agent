"""Covenant abstraction engine: credit agreement PDF -> checked covenant abstract -> human sign-off.

    from covenant_engine import abstract_deal
    abstract_deal("TRS", "data/pdf/TRS/amendment_10th-amendment-2026-03-conformed.pdf")
"""
__version__ = "0.1.0"
SPEC_VERSION = "v1.1-2026-10-13"


def abstract_deal(*args, **kwargs):
    from .abstract.new_deal import abstract_deal as _a
    return _a(*args, **kwargs)
