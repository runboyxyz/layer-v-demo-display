# LayerV Demo Display

> **DEVELOPMENT / DEMONSTRATION TOOL — NOT PART OF THE LAYERV GATEWAY**

This standalone Home Assistant App will eventually render one selected Home
Assistant dashboard locally and send only encoded image frames to a temporary,
token-protected display page. The remote viewer will never receive the Home
Assistant frontend, session, API, cookies, or credentials.

## Current Phase 1 behavior

Version 0.1.0 provides only:

- Home Assistant App packaging for `aarch64`;
- an administrator-only Home Assistant Ingress status page;
- validated future renderer settings;
- a non-root, dependency-free Python web server;
- an enforced AppArmor allowlist.

There is no Chromium package, renderer, display listener, public display URL,
LayerV API integration, or active Demo Session in this phase. Starting or
restarting the App always reports `Demo Session: Not running`.

## Installation

For local development, copy this repository to its own Git repository and add
that repository URL to the Home Assistant App Store. Install **LayerV Demo
Display**, start it, and select **Open Web UI**.

The App is independent of LayerV Gateway. Installing it does not modify Gateway
configuration, routes, data, credentials, images, or containers.

## Configuration

- `dashboard_path`: a relative local HA path such as `/demo-home/home`.
- `resolution`: `1280x720` or `1920x1080`.
- `capture_interval`: future capture interval, 1–10 seconds; default 2.
- `default_session_duration`: 15, 30, 60, or 120 minutes; default 60.
- `hide_ha_sidebar` and `hide_ha_header`: future rendering preferences.

Configuration validation rejects absolute URLs, scheme-relative URLs, query
strings, fragments, traversal, backslashes, control characters, unsupported
resolutions, and unsupported lifetimes. Phase 1 never navigates to the path.

## Removal

Stop and uninstall **LayerV Demo Display** from Home Assistant. Select removal
of App data if offered. This removes only Demo Display state. It does not alter
the LayerV Gateway or Home Assistant dashboards.

## Authentication gate

Renderer work remains blocked on an on-device authentication experiment. The
first candidate is Home Assistant's documented external-frontend authentication
bridge backed by the App-scoped Supervisor token. If that system identity does
not support a complete Lovelace session, the fallback is a dedicated non-admin
HA user authorized through the documented authorization-code flow. Username and
password browser automation is explicitly prohibited.

## Security boundary

The Ingress listener trusts only Home Assistant's Ingress proxy address and a
valid Ingress prefix. It sends no-store, restrictive CSP, no-referrer, and
nosniff headers. Request targets are not logged because Ingress paths may carry
authentication material.

Future public display service and renderer processes will use separate Unix
identities. The public process will not receive or be able to read Home
Assistant credentials. Viewer requests will return only a previously rendered
in-memory frame and will never trigger browser navigation or capture.

## Known limitations

- Phase 1 cannot render a dashboard.
- Phase 1 cannot create a Demo Session or Display URL.
- ARM64 image build and Home Assistant Green acceptance must be recorded before
  this version is considered hardware-confirmed.
- The final Chromium sandbox and AppArmor permissions are not yet established.
- Resource figures remain estimates until measured on Home Assistant Green.
