# LayerV Demo Display

> **DEVELOPMENT / DEMONSTRATION TOOL — NOT PART OF THE LAYERV GATEWAY**

This standalone Home Assistant App will eventually render one selected Home
Assistant dashboard locally and send only encoded image frames to a temporary,
token-protected display page. The remote viewer will never receive the Home
Assistant frontend, session, API, cookies, or credentials.

## Current implementation

Version 0.3.0 adds one temporary Demo Session. The Ingress administrator starts
and ends it explicitly. A dedicated non-root renderer launches Debian ARM64
Chromium, authenticates through Home Assistant's external-authentication bridge
using the App-scoped system token, restricts browser traffic to the fixed
`homeassistant:80` origin, and maintains one current in-memory JPEG.

This remains an experiment. It deliberately uses
Chromium's `--no-sandbox` mode because HA OS container namespaces must first be
measured; Chromium remains a non-root UID under the AppArmor profile. This
tradeoff must be revisited before production use. LayerV integration is not yet
implemented.

The renderer captures independently at the configured interval. Viewer
requests return only the latest existing JPEG and never cause navigation or a
new screenshot. End, expiry, renderer failure, App shutdown, and SIGTERM all
invalidate the token, erase the frame, signal the renderer, and close Chromium.

The ARM64 image is built on GitHub's native ARM64 runner and published as the
versioned `ghcr.io/runboyxyz/layer-v-demo-display` package. Home Assistant Green
pulls that image instead of compiling Chromium locally.

## Phase 1 baseline

Version 0.1.1 provides only:

- Home Assistant App packaging for `aarch64`;
- an administrator-only Home Assistant Ingress status page;
- validated future renderer settings;
- a dependency-free Python web server that reads Supervisor-owned options in a
  fixed root bootstrap and drops permanently to a dedicated non-root identity
  before opening its listener;
- an enforced AppArmor allowlist.

The Phase 1 baseline contained no Chromium package or renderer. Version 0.2.0
retains the same inactive default and adds only the bounded probe described
above. Starting or restarting the App always reports `Demo Session: Not running`.

## Installation

For local development, copy this repository to its own Git repository and add
that repository URL to the Home Assistant App Store. Install **LayerV Demo
Display**, start it, and select **Open Web UI**.

The App is independent of LayerV Gateway. Installing it does not modify Gateway
configuration, routes, data, credentials, images, or containers.

## Configuration

- `dashboard_path`: a relative local HA path such as `/demo-home/home`.
- `resolution`: `1280x720` or `1920x1080`.
- `capture_interval`: capture interval, 1–10 seconds; default 2.
- `default_session_duration`: 15, 30, 60, or 120 minutes; default 60.
- `hide_ha_sidebar` and `hide_ha_header`: accepted settings; chrome hiding is
  not yet implemented.

Configuration validation rejects absolute URLs, scheme-relative URLs, query
strings, fragments, traversal, backslashes, control characters, unsupported
resolutions, and unsupported lifetimes.

## Demo Session and viewer

Open the Ingress UI and select **Start Demo Session**. The page shows the
high-entropy display path, expiry, renderer health, last-frame age, capture
duration, approximate viewer count, and consecutive failures. Only one session
may be active. **End Demo Session** revokes it immediately.

The App maps TCP port 8099 for the token-protected viewer. Until LayerV
publication is implemented, combine the displayed path with the App host and
port for a local test, for example `http://HA-HOST:8099/display/TOKEN`. Treat
the path as a bearer secret. The page contains only minimal display HTML/JS and
JPEG pixels; it contains no HA frontend code, cookie, API URL, WebSocket
credential, iframe, or interactive dashboard surface.

## Removal

Stop and uninstall **LayerV Demo Display** from Home Assistant. Select removal
of App data if offered. This removes only Demo Display state. It does not alter
the LayerV Gateway or Home Assistant dashboards.

## Authentication result

The App-scoped Supervisor identity successfully authenticated and captured the
configured Demo Home dashboard on the HA Green. No username/password browser
automation or persistent browser profile is used.

## Security boundary

Admin endpoints trust only Home Assistant's Ingress proxy address and prefix.
Public routes accept only `/display/TOKEN` and `/display/TOKEN/frame`; the token
cannot authorize admin or renderer configuration. Tokens use
`secrets.token_urlsafe(32)`, remain only in memory, are compared in constant
time, and are never logged. Frame requests are limited to 10 per 10 seconds per
source address. Admin and viewer responses use separate restrictive CSPs plus
no-store, no-referrer, and nosniff headers.

## Known limitations

- LayerV publication is not yet implemented; port 8099 is for the current local
  viewer test.
- The renderer has three bounded frame retries but not yet one full controlled
  Chromium restart.
- HA sidebar/header hiding is not yet implemented.
- Chromium uses `--no-sandbox` inside the non-root AppArmor-confined container.
- Active resource figures and 720p/1080p comparison remain to be measured.

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
