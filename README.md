# LayerV Demo Display

> **DEVELOPMENT / DEMONSTRATION TOOL**

LayerV Demo Display is an experimental, standalone Home Assistant App intended
to display rendered pixels from one Home Assistant dashboard during demos. It
is not part of the LayerV Gateway and is not included in the production Gateway
release.

Version 0.4.0 provides one bounded Demo Session, periodic in-memory JPEG
capture, and an independently registered LayerV connector for temporary remote
display links. Links may use bearer-link access or an optional email-code gate.
Chromium is stopped whenever no session is active.

See [`layerv_demo_display/DOCS.md`](layerv_demo_display/DOCS.md) for installation,
removal, security boundaries, and known limitations.
