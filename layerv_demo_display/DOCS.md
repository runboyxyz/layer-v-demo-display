# LayerV Demo Display

> **DEVELOPMENT / DEMONSTRATION TOOL — NOT PART OF THE LAYERV GATEWAY**

This standalone Home Assistant App will eventually render one selected Home
Assistant dashboard locally and send only encoded image frames to a temporary,
token-protected display page. The remote viewer will never receive the Home
Assistant frontend, session, API, cookies, or credentials.

## Current Phase 1 behavior

Version 0.1.1 provides only:

- Home Assistant App packaging for `aarch64`;
- an administrator-only Home Assistant Ingress status page;
- validated future renderer settings;
- a dependency-free Python web server that reads Supervisor-owned options in a
  fixed root bootstrap and drops permanently to a dedicated non-root identity
  before opening its listener;
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
- The final Chromium sandbox and AppArmor permissions are not yet established.
- Resource figures remain estimates until measured on Home Assistant Green.

## Phase 1 Home Assistant Green acceptance

Phase 1 version 0.1.1 was built, installed, started, and restarted successfully
on a Home Assistant Green running Home Assistant OS 18.2, Supervisor 2026.07.5,
and Core 2026.8.1 (`aarch64`). Acceptance confirmed:

- Home Assistant recognizes the App as experimental and AppArmor/Ingress
  enabled;
- Ingress displays `Demo Session: Not running`;
- the renderer is not installed and Chromium is not running;
- restart returns to the same inactive state;
- reported idle CPU was 0%;
- reported idle RAM was approximately 31.9 MB;
- the existing LayerV Gateway remained running throughout installation,
  troubleshooting, restart, and Ingress testing.

The initial 0.1.0 package revealed that Supervisor-owned `/data/options.json`
is not readable by a container launched directly as the runtime UID. Version
0.1.1 reads options in a fixed root bootstrap and permanently drops to UID/GID
2200 before creating the HTTP listener. Nine unit/security tests cover the
configuration boundary, inactive status, Ingress trust, security headers, and
identity drop. Historical 0.1.0 tracebacks may remain visible in Home Assistant
logs when App data is retained; a later successful Phase 1 startup entry is the
authoritative acceptance signal.
