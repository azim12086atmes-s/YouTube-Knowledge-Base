#!/usr/bin/env python
"""takeout_sample.py — back-compat shim for url_source.py.

ponytail: the old `bin/takeout_sample.py` was renamed to `bin/url_source.py`
when the xlsx/urlfile sources landed. This shim re-exports the public surface
so any old shell alias / docs reference keeps working. New code should call
`bin/url_source.py` directly.

Usage:
    python takeout_sample.py [--source takeout-watch] [--n 6]
"""
import sys
from pathlib import Path

# ponytail: same directory — exec the real file in our namespace.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import url_source as _real  # noqa: E402

main = _real.main
slug_from_url = _real.slug_from_url
source_takeout_watch = _real.source_takeout_watch
source_takeout_watch_all = _real.source_takeout_watch_all
sample = _real.sample
emit = _real.emit

if __name__ == "__main__":
    sys.exit(_real.main())
