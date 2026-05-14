"""Browser-facing dashboard for live + historical workflow inspection.

This subpackage is loaded lazily by ``zeperion serve`` so the CLI
boot path doesn't pull in FastAPI/uvicorn on every invocation.
Install the optional dependency group to enable it:

    pip install 'zeperion[web]'

The HTTP surface is intentionally small — see :mod:`zeperion.web.app`
(``create_app``) for the full API. Single-file HTML/CSS/JS via
inlined Jinja templates keeps the deployment artifact zero-config
(no JS build step, no static asset pipeline, no ``package_data``
template lookup that breaks with editable installs).
"""

from __future__ import annotations
