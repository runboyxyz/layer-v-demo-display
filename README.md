# LayerV Demo Display

> **DEVELOPMENT / DEMONSTRATION TOOL**

LayerV Demo Display is an experimental, standalone Home Assistant App intended
to display rendered pixels from one Home Assistant dashboard during demos. It
is not part of the LayerV Gateway and is not included in the production Gateway
release.

Version 0.3.0 provides one bounded Demo Session, periodic in-memory JPEG
capture, and a token-only read-only viewer. Chromium is stopped whenever no
session is active. LayerV publication remains a separate later phase.

See [`layerv_demo_display/DOCS.md`](layerv_demo_display/DOCS.md) for installation,
removal, security boundaries, and known limitations.
