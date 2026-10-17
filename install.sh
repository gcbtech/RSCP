#!/usr/bin/env bash
#
# RSCP installer / upgrader (Debian 12+ or Ubuntu 24.04+, Python >= 3.11).
#
#   Fresh install (stable):
#     curl -sSL https://raw.githubusercontent.com/gcbtech/RSCP/main/install.sh | sudo bash
#   With options:
#     curl -sSL .../install.sh | sudo bash -s -- --channel beta --noninteractive
#
# Layout:
#   /opt/rscp/releases/<version>/   code + its own venv (root-owned, read-only to the app)
#   /opt/rscp/current               symlink to the active release (switched atomically)
#   /opt/rscp/previous              symlink to the release that was active before
#   /opt/rscp/bin/rscp-update       installed copy of this script (used by the updater)
#   /var/lib/rscp/                  rscp.db, config.json, uploads/, backups/ (user rscp)
#   /etc/default/rscp               optional environment (RSCP_BIND=host:port)
#   rscp.service                    gunicorn as user rscp; logs in journald
#   rscp-update.path/.service       applies update/rollback requests from the web UI
#
# Options:
#   --channel stable|beta   stable = newest GitHub release tag (default), beta = beta branch
#   --version vX.Y.Z        install that tag
#   --branch NAME           install the tip of a branch
#   --source DIR            install from a local source tree instead of GitHub
#   --bind HOST:PORT        listen address (written to /etc/default/rscp; default 0.0.0.0:5000)
#   --noninteractive, -y    never prompt (implied when there is no terminal)
#   --upgrade               require an existing install (used by the updater)
#   --rollback              switch back to the previous release
#   --migrate-from DIR      one-time move of a 2.x install (e.g. /opt/ebay_receiver)
#   --legacy-unit NAME      systemd unit of that 2.x install (default: rscp)
#   --from-request FILE     read action/channel from a web UI request (rscp-update.service)
#   -h, --help
#
# Re-running is safe: an existing install is upgraded (DB backed up first,
# automatic rollback if the new release fails its health check).
#
# Everything lives in functions and only runs from the last line, so a
# truncated download cannot execute half an install.

set -euo pipefail

main() {
    readonly REPO="gcbtech/RSCP"
    readonly PREFIX="/opt/rscp"
    readonly RELEASES="$PREFIX/releases"
    readonly CURRENT="$PREFIX/current"
    readonly PREVIOUS="$PREFIX/previous"
    readonly BIN_DIR="$PREFIX/bin"
    readonly LEGACY_BACKUP_DIR="$PREFIX/legacy"
    readonly DATA_DIR="/var/lib/rscp"
    readonly STATUS_FILE="$DATA_DIR/update-status.json"
    readonly ENV_FILE="/etc/default/rscp"
    readonly UNIT_DIR="/etc/systemd/system"
    readonly SERVICE_USER="rscp"
    readonly UNIT="rscp"
    readonly KEEP_RELEASES=5
    readonly KEEP_DB_BACKUPS=5
    readonly HEALTH_TIMEOUT=120
    export DEBIAN_FRONTEND=noninteractive
    umask 022

    CHANNEL=""
    VERSION_TAG=""
    BRANCH=""
    SOURCE_DIR=""
    BIND=""
    NONINTERACTIVE=0
    UPGRADE=0
    ROLLBACK=0
    MIGRATE_FROM=""
    LEGACY_UNIT="rscp"
    REQUEST_FILE=""
    REQUEST_ID=""
    MIGRATE_NEEDED=0
    DB_BACKUP=""
    ACTION="install"
    BUILD_DIR=""
    STAGING_DIR=""
    STATUS_ENABLED=0

    setup_colors
    parse_args "$@"

    [ "$(id -u)" -eq 0 ] || die "Please run as root (sudo)."
    cd /
    take_lock
    trap cleanup EXIT
    STATUS_ENABLED=1

    if [ -n "$REQUEST_FILE" ]; then
        read_request "$REQUEST_FILE" || exit 0
    fi

    if [ "$ROLLBACK" -eq 1 ]; then
        ACTION="rollback"
        run_rollback
    else
        [ "$UPGRADE" -eq 1 ] && ACTION="upgrade"
        run_install
    fi
}

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

setup_colors() {
    if [ -t 1 ]; then
        C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[1;33m'
        C_BLUE=$'\033[0;34m'; C_OFF=$'\033[0m'
    else
        C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_OFF=""
    fi
}

log()  { printf '%s==>%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%sWARNING:%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die() {
    printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2
    write_status error "" "$*"
    exit 1
}

usage() {
    sed -n '2,/^# Everything lives/p' "${BASH_SOURCE[0]}" 2>/dev/null | sed 's/^# \{0,1\}//' \
        || echo "See the header of install.sh for usage."
}

# Progress for the web UI (Admin > System Updates polls this file).
# Written atomically via mktemp + rename, so a symlink planted in the
# rscp-writable data dir is replaced, never followed.
write_status() {
    local state="$1" progress="${2:-}" error="${3:-}" version="${4:-}"
    [ "${STATUS_ENABLED:-0}" -eq 1 ] && [ -d "$DATA_DIR" ] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    local tmp
    tmp=$(mktemp "$DATA_DIR/.update-status.XXXXXX" 2>/dev/null) || return 0
    if python3 -I - "$state" "$progress" "$error" "$version" "$REQUEST_ID" "$ACTION" >"$tmp" 2>/dev/null <<'PY'
import json, sys, time
state, progress, error, version, rid, action = sys.argv[1:7]
print(json.dumps({"status": state, "progress": progress, "error": error or None,
                  "version": version or None, "request_id": rid or None,
                  "action": action, "updated_at": int(time.time())}))
PY
    then
        chmod 644 "$tmp" && mv -f "$tmp" "$STATUS_FILE" || rm -f "$tmp"
    else
        rm -f "$tmp"
    fi
    return 0
}

step() {  # step "message": log it and report it as progress
    log "$1"
    write_status updating "$1"
}

# --------------------------------------------------------------------------
# Arguments, locking, cleanup
# --------------------------------------------------------------------------

parse_args() {
    local sources=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --channel)        need_arg "$@"; CHANNEL="$2"; sources=$((sources + 1)); shift 2 ;;
            --version)        need_arg "$@"; VERSION_TAG="$2"; sources=$((sources + 1)); shift 2 ;;
            --branch)         need_arg "$@"; BRANCH="$2"; sources=$((sources + 1)); shift 2 ;;
            --source)         need_arg "$@"; SOURCE_DIR="$2"; sources=$((sources + 1)); shift 2 ;;
            --bind)           need_arg "$@"; BIND="$2"; shift 2 ;;
            --migrate-from)   need_arg "$@"; MIGRATE_FROM="$2"; shift 2 ;;
            --legacy-unit)    need_arg "$@"; LEGACY_UNIT="${2%.service}"; shift 2 ;;
            --from-request)   need_arg "$@"; REQUEST_FILE="$2"; shift 2 ;;
            --upgrade)        UPGRADE=1; NONINTERACTIVE=1; shift ;;
            --rollback)       ROLLBACK=1; NONINTERACTIVE=1; shift ;;
            --noninteractive|--non-interactive|-y) NONINTERACTIVE=1; shift ;;
            -h|--help)        usage; exit 0 ;;
            *)                die "Unknown option: $1 (see --help)" ;;
        esac
    done
    [ "$sources" -le 1 ] || die "Use only one of --channel, --version, --branch, --source."
    if [ -n "$CHANNEL" ]; then
        case "$CHANNEL" in stable|beta) ;; *) die "--channel must be stable or beta" ;; esac
    fi
    if [ -n "$VERSION_TAG" ]; then
        VERSION_TAG="v${VERSION_TAG#v}"
        valid_tag "$VERSION_TAG" || die "--version must look like v3.0.1"
    fi
    if [ -n "$BRANCH" ]; then
        valid_branch "$BRANCH" || die "Invalid branch name: $BRANCH"
    fi
    if [ -n "$BIND" ]; then
        valid_bind "$BIND" || die "--bind must look like 0.0.0.0:5000 or 127.0.0.1:5000"
    fi
    if [ -n "$MIGRATE_FROM" ] && [ "$ROLLBACK" -eq 1 ]; then
        die "--migrate-from cannot be combined with --rollback"
    fi
    case "$LEGACY_UNIT" in *[!A-Za-z0-9_.@-]*|"") die "Invalid --legacy-unit" ;; esac
}

need_arg() { [ $# -ge 2 ] && [ -n "$2" ] || die "$1 needs a value"; }
valid_tag() { [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; }
valid_branch() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$ ]] && [[ "$1" != *..* ]]; }
valid_bind() { local re='^[][0-9A-Za-z.:-]*:[0-9]{1,5}$'; [[ "$1" =~ $re ]]; }

take_lock() {
    mkdir -p /run/lock
    exec 9>/run/lock/rscp-install.lock
    if ! flock -n 9; then
        echo "Another RSCP install/update is already running." >&2
        exit 1
    fi
}

cleanup() {
    local rc=$?
    [ -n "$BUILD_DIR" ] && [ -d "$BUILD_DIR" ] && rm -rf "$BUILD_DIR"
    [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ] && rm -rf "$STAGING_DIR"
    exit "$rc"
}

can_prompt() {
    [ "$NONINTERACTIVE" -eq 0 ] && { : </dev/tty; } 2>/dev/null
}

# --------------------------------------------------------------------------
# Web UI request (rscp-update.service). The file is written by the
# unprivileged web process, so it is parsed defensively and every value
# is validated against a whitelist before use.
# --------------------------------------------------------------------------

read_request() {
    local file="$1" parsed
    if [ ! -f "$file" ] || [ -L "$file" ]; then
        echo "No update request at $file; nothing to do."
        return 1
    fi
    parsed=$(python3 -I - "$file" <<'PY'
import json, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
with os.fdopen(fd) as f:
    data = json.loads(f.read(4096) or "{}")
def val(key):
    v = data.get(key)
    return "" if v is None else str(v).replace("\n", " ")[:80]
for key in ("id", "action", "channel", "version"):
    print(val(key))
print("end")  # keeps empty trailing values from being stripped by $(...)
PY
    ) || { rm -f "$file"; die "Unreadable update request; ignored."; }
    parsed="${parsed//$'\r'/}"
    # Consume the request first so a crash can never re-run it in a loop.
    rm -f "$file"

    local rid action channel version
    { IFS= read -r rid; IFS= read -r action; IFS= read -r channel; IFS= read -r version; } <<<"$parsed"
    [[ "$rid" =~ ^[A-Za-z0-9-]{0,64}$ ]] || rid=""
    REQUEST_ID="$rid"
    NONINTERACTIVE=1
    case "$action" in
        upgrade)
            ACTION="upgrade"; UPGRADE=1; ROLLBACK=0; CHANNEL=""; VERSION_TAG=""; BRANCH=""; SOURCE_DIR=""
            if [ -n "$version" ]; then
                version="v${version#v}"
                valid_tag "$version" || die "Rejected update request: bad version '$version'"
                VERSION_TAG="$version"
            else
                case "$channel" in
                    stable|beta) CHANNEL="$channel" ;;
                    *) die "Rejected update request: bad channel '$channel'" ;;
                esac
            fi
            ;;
        rollback) ACTION="rollback"; ROLLBACK=1 ;;
        *) die "Rejected update request: unknown action '$action'" ;;
    esac
    echo "Processing web UI request ${REQUEST_ID:-?}: $action ${VERSION_TAG:-$CHANNEL}"
    return 0
}

# --------------------------------------------------------------------------
# System checks and packages
# --------------------------------------------------------------------------

check_os() {
    [ -r /etc/os-release ] || die "Cannot detect the OS (/etc/os-release missing). Debian or Ubuntu required."
    local id like
    id=$(. /etc/os-release && echo "${ID:-}")
    like=$(. /etc/os-release && echo "${ID_LIKE:-}")
    case " $id $like " in
        *" debian "*|*" ubuntu "*) ;;
        *) die "Unsupported OS '$id'. RSCP's installer supports Debian and Ubuntu (or derivatives)." ;;
    esac
    [ -d /run/systemd/system ] || die "systemd is not running; RSCP needs systemd."
}

ensure_packages() {
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    command -v curl >/dev/null 2>&1 || missing+=(curl)
    command -v tar >/dev/null 2>&1 || missing+=(tar)
    command -v flock >/dev/null 2>&1 || missing+=(util-linux)
    [ -e /etc/ssl/certs/ca-certificates.crt ] || missing+=(ca-certificates)
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || missing+=(python3-venv)
    else
        missing+=(python3-venv)
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        step "Installing system packages: ${missing[*]}"
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends "${missing[@]}"
    fi
    python3 - <<'PY' || die "Python 3.11 or newer is required (found $(python3 --version 2>&1))."
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
}

# --------------------------------------------------------------------------
# Choosing what to install
# --------------------------------------------------------------------------

choose_channel_interactively() {
    if [ -z "$CHANNEL$VERSION_TAG$BRANCH$SOURCE_DIR" ] && can_prompt; then
        echo
        echo "${C_BLUE}Which release channel?${C_OFF}"
        echo "  1) stable - latest release (recommended)"
        echo "  2) beta   - newest features, may have bugs"
        local choice=""
        read -r -p "Choice [1]: " choice </dev/tty || true
        case "$choice" in 2|beta) CHANNEL="beta" ;; *) CHANNEL="stable" ;; esac
    fi
    if [ -z "$CHANNEL$VERSION_TAG$BRANCH$SOURCE_DIR" ]; then
        CHANNEL="stable"
    fi
}

latest_stable_tag() {
    local api="https://api.github.com/repos/$REPO" tag=""
    tag=$(curl -fsSL --max-time 20 -H 'Accept: application/vnd.github+json' "$api/releases/latest" 2>/dev/null \
        | python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("tag_name") or "")' 2>/dev/null) || tag=""
    if ! valid_tag "$tag"; then
        tag=$(curl -fsSL --max-time 20 -H 'Accept: application/vnd.github+json' "$api/tags?per_page=100" 2>/dev/null \
            | python3 -I -c '
import json, re, sys
best = None
for t in json.load(sys.stdin):
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", t.get("name", ""))
    if m:
        key = tuple(int(x) for x in m.groups())
        if best is None or key > best[0]:
            best = (key, t["name"])
print(best[1] if best else "")' 2>/dev/null) || tag=""
    fi
    valid_tag "$tag" && echo "$tag"
    return 0
}

# Sets SRC_KIND (tag|branch|local), SRC_REF and SRC_URL.
resolve_source() {
    SRC_URL=""
    if [ -n "$SOURCE_DIR" ]; then
        SRC_KIND="local"
        SRC_REF="$(readlink -f "$SOURCE_DIR")" || die "--source $SOURCE_DIR does not exist"
        [ -d "$SRC_REF" ] || die "--source $SOURCE_DIR is not a directory"
        return
    fi
    if [ -n "$VERSION_TAG" ]; then
        SRC_KIND="tag"; SRC_REF="$VERSION_TAG"
    elif [ -n "$BRANCH" ]; then
        SRC_KIND="branch"; SRC_REF="$BRANCH"
    elif [ "$CHANNEL" = "beta" ]; then
        SRC_KIND="branch"; SRC_REF="beta"
    else
        local tag
        tag=$(latest_stable_tag)
        if [ -n "$tag" ]; then
            SRC_KIND="tag"; SRC_REF="$tag"
        else
            warn "No release tags found on GitHub; using the main branch for the stable channel."
            SRC_KIND="branch"; SRC_REF="main"
        fi
    fi
    if [ "$SRC_KIND" = "tag" ]; then
        SRC_URL="https://github.com/$REPO/archive/refs/tags/$SRC_REF.tar.gz"
    else
        SRC_URL="https://github.com/$REPO/archive/refs/heads/$SRC_REF.tar.gz"
    fi
}

describe_source() {
    case "$SRC_KIND" in
        local) echo "local tree $SRC_REF" ;;
        tag) echo "release $SRC_REF" ;;
        branch) echo "branch $SRC_REF" ;;
    esac
}

# --------------------------------------------------------------------------
# Building a release
# --------------------------------------------------------------------------

copy_local_tree() {  # copy_local_tree SRC DEST: code only, never state or junk
    local src="$1" dest="$2" archive="$2.src.tar" rc=0
    tar -C "$src" -cf "$archive" \
        --anchored \
        --exclude=./.git \
        --exclude='./tests/*.db' \
        --exclude=./config.json \
        --exclude='./config.json.*' \
        --exclude='./rscp.db*' \
        --exclude=./manifest.csv \
        --exclude=./static/uploads \
        --exclude=./temp \
        --exclude=./backups \
        --exclude=./update_status.json \
        --exclude=./.background_tasks.lock \
        --exclude=./ignore \
        --no-anchored \
        --exclude=venv \
        --exclude=.venv \
        --exclude=__pycache__ \
        --exclude=.pytest_cache \
        --exclude='*.log' \
        --exclude='*.log.[0-9]*' \
        --exclude='_*backup*' \
        --exclude='*.bak*' \
        --exclude='.sysmigrate_*' \
        . || rc=$?
    # tar exits 1 when a file changed while being read (a live tree); only
    # higher codes are real failures.
    if [ "$rc" -gt 1 ]; then
        rm -f "$archive"
        die "Could not read the source tree $src (tar exit $rc); nothing was changed."
    fi
    [ "$rc" -eq 1 ] && warn "Some files in $src changed while being copied; continuing."
    if ! tar -C "$dest" -xf "$archive" --no-same-owner; then
        rm -f "$archive"
        die "Could not unpack the copy of $src; nothing was changed."
    fi
    rm -f "$archive"
    # A 2.x release dir links state into its shared dir; never carry a
    # symlink that points outside the new release.
    local link target
    while IFS= read -r -d '' link; do
        target=$(readlink -f "$link" 2>/dev/null || true)
        case "$target" in
            "$dest"/*) ;;
            *) warn "Dropping external symlink ${link#"$dest"/} -> $(readlink "$link")"; rm -f "$link" ;;
        esac
    done < <(find "$dest" -type l -print0)
}

fetch_source() {  # fetch_source DEST
    local dest="$1"
    if [ "$SRC_KIND" = "local" ]; then
        step "Copying source tree from $SRC_REF..."
        copy_local_tree "$SRC_REF" "$dest"
    else
        step "Downloading $(describe_source) from GitHub..."
        local archive="$dest.tar.gz"
        curl -fSL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 900 -o "$archive" "$SRC_URL" \
            || { rm -f "$archive"; die "Download failed: $SRC_URL"; }
        tar -xzf "$archive" -C "$dest" --strip-components=1 --no-same-owner \
            || { rm -f "$archive"; die "Could not unpack $SRC_URL"; }
        rm -f "$archive"
    fi
    local entry
    for entry in wsgi.py app/__init__.py app/migrate.py VERSION gunicorn.conf.py deploy/rscp.service install.sh; do
        [ -e "$dest/$entry" ] || die "The downloaded code is missing $entry; aborting (nothing was changed)."
    done
    [ -e "$dest/requirements.lock" ] || [ -e "$dest/requirements.txt" ] \
        || die "The downloaded code has no requirements file; aborting (nothing was changed)."
    NEW_VERSION=$(tr -d ' \r\n' <"$dest/VERSION")
    [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] \
        || die "Unexpected VERSION '$NEW_VERSION' in the downloaded code."
}

build_venv() {  # build_venv RELEASE_DIR
    local dir="$1" req
    if [ -f "$dir/requirements.lock" ]; then
        req="$dir/requirements.lock"
    else
        req="$dir/requirements.txt"
        warn "requirements.lock not found; installing from requirements.txt (unpinned)."
    fi
    step "Installing dependencies into a new virtualenv ($(basename "$req"))..."
    python3 -m venv "$dir/venv"
    "$dir/venv/bin/python" -m pip install --quiet --no-cache-dir --disable-pip-version-check \
        --no-input -r "$req" || die "Installing Python dependencies failed (see above); nothing was changed."
    "$dir/venv/bin/python" -m compileall -q "$dir/app" "$dir/wsgi.py" "$dir/gunicorn.conf.py" >/dev/null 2>&1 || true

    step "Checking that the new release imports cleanly..."
    local probe
    probe=$(mktemp -d)
    if ! (cd "$dir" && env -i PATH=/usr/bin:/bin HOME="$probe" RSCP_DATA_DIR="$probe" \
            "$dir/venv/bin/python" -c 'import gunicorn, app, app.migrate' >"$probe/out" 2>&1); then
        cat "$probe/out" >&2
        rm -rf "$probe"
        die "The new release failed to import; nothing was changed."
    fi
    rm -rf "$probe"
}

build_release() {
    mkdir -p "$RELEASES" "$BIN_DIR"
    chmod 755 "$PREFIX" "$RELEASES" "$BIN_DIR"
    BUILD_DIR=$(mktemp -d "$RELEASES/.build-XXXXXX")
    fetch_source "$BUILD_DIR"
    build_venv "$BUILD_DIR"

    local sha=""
    if [ "$SRC_KIND" = "local" ] && [ -d "$SRC_REF/.git" ] && command -v git >/dev/null 2>&1; then
        sha=$(git -c safe.directory='*' -C "$SRC_REF" rev-parse --short HEAD 2>/dev/null || true)
    fi
    python3 -I - "$BUILD_DIR/.release-info" "$NEW_VERSION" "$SRC_KIND" "$SRC_REF" "$sha" <<'PY'
import json, sys, time
path, version, kind, ref, sha = sys.argv[1:6]
with open(path, "w") as f:
    json.dump({"version": version, "source": kind, "ref": ref, "commit": sha or None,
               "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f, indent=2)
PY
    chown -R root:root "$BUILD_DIR"
    chmod -R u+rwX,go+rX,go-w "$BUILD_DIR"

    local name="$NEW_VERSION"
    [ -e "$RELEASES/$name" ] && name="$NEW_VERSION-$(date +%Y%m%d%H%M%S)"
    mv "$BUILD_DIR" "$RELEASES/$name"
    # The venv was created inside the build dir; its scripts' shebangs and
    # activate files name that path. Point them at the final location.
    grep -rlIF "$BUILD_DIR" "$RELEASES/$name/venv/bin" 2>/dev/null \
        | xargs -r sed -i "s|$BUILD_DIR/venv|$RELEASES/$name/venv|g"
    BUILD_DIR=""
    NEW_RELEASE="$RELEASES/$name"
    info "Built $NEW_RELEASE"
}

current_release() { [ -L "$CURRENT" ] && readlink -f "$CURRENT" || true; }
release_version() { tr -d ' \r\n' <"$1/VERSION" 2>/dev/null || echo "unknown"; }

point_link() {  # point_link TARGET LINK: atomic symlink switch
    local tmp="$2.new.$$"
    ln -sfn "$1" "$tmp"
    mv -Tf "$tmp" "$2"
}

activate_release() {  # activate_release NEW [OLD]
    local new="$1" old="${2:-}"
    if [ -n "$old" ] && [ "$old" != "$new" ]; then
        point_link "$old" "$PREVIOUS"
    fi
    point_link "$new" "$CURRENT"
}

prune_releases() {
    local cur prev count=0 dir
    cur=$(current_release)
    prev=$([ -L "$PREVIOUS" ] && readlink -f "$PREVIOUS" || true)
    while IFS= read -r dir; do
        dir="${dir%/}"
        count=$((count + 1))
        [ "$count" -le "$KEEP_RELEASES" ] && continue
        [ "$dir" = "$cur" ] || [ "$dir" = "$prev" ] && continue
        info "Removing old release $(basename "$dir")"
        rm -rf -- "$dir"
    done < <(ls -1dt "$RELEASES"/*/ 2>/dev/null)
    return 0
}

# --------------------------------------------------------------------------
# Service user, data directory, config, units
# --------------------------------------------------------------------------

ensure_user() {
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log "Creating system user $SERVICE_USER"
        useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
}

ensure_data_dir() {
    install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
    local sub
    for sub in uploads backups tmp; do
        if [ ! -d "$DATA_DIR/$sub" ]; then
            install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/$sub"
        fi
    done
}

as_rscp() { runuser -u "$SERVICE_USER" -- "$@"; }

ensure_config() {
    # Runs as the service user: config.json lives in a directory that user
    # controls, so root never opens it.
    if ! as_rscp python3 -I - "$DATA_DIR/config.json" <<'PY'
import json, os, secrets, sys, tempfile
path = sys.argv[1]
conf = {}
if os.path.exists(path):
    with open(path) as f:
        conf = json.load(f)
changed = not os.path.exists(path)
if not conf.get("SECRET_KEY"):
    conf["SECRET_KEY"] = secrets.token_hex(32)
    changed = True
if changed:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(conf, f, indent=4)
    os.replace(tmp, path)
    print("    wrote config.json (SECRET_KEY generated)")
os.chmod(path, 0o600)
PY
    then
        die "Could not read or write $DATA_DIR/config.json (is it valid JSON?)."
    fi
}

ensure_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        printf '# RSCP service environment (read by rscp.service and the updater)\nRSCP_BIND=%s\n' \
            "${BIND:-0.0.0.0:5000}" >"$ENV_FILE"
        chmod 644 "$ENV_FILE"
    elif [ -n "$BIND" ]; then
        if grep -q '^RSCP_BIND=' "$ENV_FILE"; then
            sed -i "s|^RSCP_BIND=.*|RSCP_BIND=$BIND|" "$ENV_FILE"
        else
            echo "RSCP_BIND=$BIND" >>"$ENV_FILE"
        fi
    fi
}

health_url() {
    local bind host port
    bind=$(sed -n 's/^RSCP_BIND=//p' "$ENV_FILE" 2>/dev/null | tail -n 1 | tr -d "\"' ")
    valid_bind "${bind:-x}" || bind="0.0.0.0:5000"
    port="${bind##*:}"
    host="${bind%:*}"
    case "$host" in ""|0.0.0.0|"[::]"|"::") host="127.0.0.1" ;; esac
    echo "http://$host:$port/healthz"
}

install_units() {  # install_units RELEASE_DIR
    local rel="$1" f changed=0
    for f in rscp.service rscp-update.service rscp-update.path; do
        if [ -f "$rel/deploy/$f" ]; then
            if ! cmp -s "$rel/deploy/$f" "$UNIT_DIR/$f"; then
                install -m 644 "$rel/deploy/$f" "$UNIT_DIR/$f"
                changed=1
            fi
        fi
    done
    if [ -f "$rel/install.sh" ]; then
        install -m 755 "$rel/install.sh" "$BIN_DIR/.rscp-update.new"
        mv -f "$BIN_DIR/.rscp-update.new" "$BIN_DIR/rscp-update"
    fi
    systemctl daemon-reload
    [ "$changed" -eq 1 ] && info "Installed systemd units from $(basename "$rel")"
    return 0
}

enable_units() {
    systemctl enable --quiet "$UNIT.service" rscp-update.path
    systemctl start rscp-update.path
}

backup_db() {
    [ -f "$DATA_DIR/rscp.db" ] || return 0
    local label="$1"
    step "Backing up the database..."
    # As the service user (the backups dir is theirs), via SQLite's online
    # backup API: consistent even while the app is writing in WAL mode.
    DB_BACKUP=$(as_rscp python3 -I - "$DATA_DIR/rscp.db" "$DATA_DIR/backups" "$label" "$KEEP_DB_BACKUPS" <<'PY'
import glob, os, sqlite3, sys, time
db, root, label, keep = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
os.makedirs(root, exist_ok=True)
dest = os.path.join(root, "pre-update-%s-%s.db" % (label, time.strftime("%Y%m%d-%H%M%S")))
src = sqlite3.connect(db, timeout=30)
dst = sqlite3.connect(dest)
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
old = sorted(glob.glob(os.path.join(root, "pre-update-*.db")), key=os.path.getmtime, reverse=True)
for path in old[keep:]:
    os.remove(path)
print(dest)
PY
    ) || die "Database backup failed; nothing was changed."
    info "Database backup: $DB_BACKUP"
}

# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------

wait_healthy() {  # wait_healthy EXPECTED_VERSION
    local expected="$1" url body deadline
    url=$(health_url)
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if body=$(curl -fsS --max-time 5 "$url" 2>/dev/null); then
            if [ -z "$expected" ] || printf '%s' "$body" | grep -q "\"version\": *\"$expected\""; then
                info "Healthy: $url -> $body"
                return 0
            fi
        fi
        if systemctl is-failed --quiet "$UNIT.service"; then
            break
        fi
        sleep 2
    done
    warn "Health check failed at $url"
    journalctl -u "$UNIT" -n 30 --no-pager 2>/dev/null || true
    return 1
}

restart_and_check() {  # restart_and_check EXPECTED_VERSION
    write_status restarting "Restarting RSCP on v$1..." "" "$1"
    log "Restarting $UNIT (schema migrations run first)..."
    systemctl restart "$UNIT.service" || true
    wait_healthy "$1"
}

# --------------------------------------------------------------------------
# One-time migration from a 2.x install
# --------------------------------------------------------------------------

# A 2.x install (unit without RSCP_DATA_DIR) and no data here yet: a plain
# install would replace its unit and start an EMPTY store. Require an
# explicit --migrate-from instead.
refuse_unmigrated_legacy() {
    [ -e "$DATA_DIR/rscp.db" ] && return 0
    local unit_file workdir
    unit_file=$(legacy_unit_file)
    [ -n "$unit_file" ] && [ -f "$unit_file" ] || return 0
    grep -q 'RSCP_DATA_DIR=' "$unit_file" && return 0
    workdir=$(sed -n 's/^WorkingDirectory=//p' "$unit_file" | head -n 1)
    die "Found an existing RSCP 2.x service ($unit_file${workdir:+, app in $workdir}).
    Re-run with --migrate-from ${workdir:-<its app directory>} to move its data into $DATA_DIR,
    or remove that service first if you really want a fresh, empty install."
}

legacy_unit_file() {
    systemctl show -p FragmentPath --value "$LEGACY_UNIT.service" 2>/dev/null || true
}

# Validates the legacy install before anything is built or stopped.
# Sets MIGRATE_NEEDED=0 when this data dir was already migrated.
check_migration() {
    MIGRATE_NEEDED=1
    [ -d "$MIGRATE_FROM" ] || die "--migrate-from $MIGRATE_FROM is not a directory"
    LEGACY_REAL=$(readlink -f "$MIGRATE_FROM")
    if [ -f "$DATA_DIR/.migrated-from" ]; then
        info "Data was already migrated ($(head -n 1 "$DATA_DIR/.migrated-from")); skipping the data copy."
        MIGRATE_NEEDED=0
        return
    fi
    [ -e "$DATA_DIR/rscp.db" ] && die "$DATA_DIR/rscp.db already exists; refusing to overwrite it with $MIGRATE_FROM's database."
    LEGACY_DB=$(readlink -f "$MIGRATE_FROM/rscp.db" || true)
    [ -n "$LEGACY_DB" ] && [ -f "$LEGACY_DB" ] || die "No rscp.db found in $MIGRATE_FROM"
    LEGACY_CONFIG=$(readlink -f "$MIGRATE_FROM/config.json" || true)
    [ -n "$LEGACY_CONFIG" ] && [ -f "$LEGACY_CONFIG" ] || die "No config.json found in $MIGRATE_FROM"
    LEGACY_MANIFEST=$(readlink -f "$MIGRATE_FROM/manifest.csv" || true)
    [ -f "$LEGACY_MANIFEST" ] || LEGACY_MANIFEST=""
    LEGACY_UPLOADS=$(readlink -f "$MIGRATE_FROM/static/uploads" || true)
    [ -d "$LEGACY_UPLOADS" ] || LEGACY_UPLOADS=""

    # Enough space for the database + uploads (+10% slack)?
    mkdir -p "$(dirname "$DATA_DIR")"
    local need avail
    need=$(du -sk "$LEGACY_DB" ${LEGACY_UPLOADS:+"$LEGACY_UPLOADS"} 2>/dev/null | awk '{s+=$1} END {print int(s*1.1)+1024}')
    avail=$(df -Pk "$(dirname "$DATA_DIR")" | awk 'NR==2 {print $4}')
    [ "$avail" -gt "$need" ] || die "Not enough disk space for the migration (need ~${need}K, have ${avail}K)."

    LEGACY_UNIT_FILE=$(legacy_unit_file)
    if [ -n "$LEGACY_UNIT_FILE" ] && [ -f "$LEGACY_UNIT_FILE" ] && grep -q 'RSCP_DATA_DIR=' "$LEGACY_UNIT_FILE"; then
        die "Unit $LEGACY_UNIT already uses the new layout but $DATA_DIR has no migration marker; check manually."
    fi
    info "Legacy install: $MIGRATE_FROM -> $LEGACY_REAL"
    info "  database: $LEGACY_DB"
    info "  config:   $LEGACY_CONFIG"
    info "  manifest: ${LEGACY_MANIFEST:-none}"
    info "  uploads:  ${LEGACY_UPLOADS:-none}"
    info "  unit:     $LEGACY_UNIT (${LEGACY_UNIT_FILE:-not found})"
}

# Saves the legacy unit, stops the service, copies the data. If anything
# fails before the new service is installed, the legacy service is started
# again and the partial copy discarded.
migrate_legacy_data() {
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    LEGACY_SAVED_UNIT=""
    LEGACY_SAVED_DROPIN=""
    mkdir -p "$LEGACY_BACKUP_DIR"
    chmod 700 "$LEGACY_BACKUP_DIR"

    if [ -n "$LEGACY_UNIT_FILE" ] && [ -f "$LEGACY_UNIT_FILE" ]; then
        LEGACY_SAVED_UNIT="$LEGACY_BACKUP_DIR/$LEGACY_UNIT.service.$ts"
        cp -a "$LEGACY_UNIT_FILE" "$LEGACY_SAVED_UNIT"
        if [ -d "$UNIT_DIR/$LEGACY_UNIT.service.d" ]; then
            LEGACY_SAVED_DROPIN="$LEGACY_BACKUP_DIR/$LEGACY_UNIT.service.d.$ts"
            cp -a "$UNIT_DIR/$LEGACY_UNIT.service.d" "$LEGACY_SAVED_DROPIN"
        fi
        write_legacy_restore_script "$ts"
    fi

    step "Stopping the legacy service ($LEGACY_UNIT)..."
    systemctl stop "$LEGACY_UNIT.service" 2>/dev/null || true
    if systemctl is-active --quiet "$LEGACY_UNIT.service"; then
        die "Could not stop $LEGACY_UNIT; nothing was copied."
    fi

    step "Copying the database (SQLite backup API)..."
    STAGING_DIR=$(mktemp -d "$DATA_DIR/.migrate-XXXXXX")
    if ! python3 -I - "$LEGACY_DB" "$STAGING_DIR/rscp.db" <<'PY'
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
try:
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True, timeout=30)
    src.execute("SELECT count(*) FROM sqlite_master").fetchone()
except sqlite3.Error:
    src = sqlite3.connect(src_path, timeout=30)   # WAL db without -shm needs rw
dst = sqlite3.connect(dst_path)
src.backup(dst)
src.close()
result = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
if result != "ok":
    sys.exit("integrity_check: %s" % result)
PY
    then
        migration_failed "Database copy failed."
    fi
    cp "$LEGACY_CONFIG" "$STAGING_DIR/config.json" || migration_failed "Copying config.json failed."
    if [ -n "$LEGACY_MANIFEST" ]; then
        cp -p "$LEGACY_MANIFEST" "$STAGING_DIR/manifest.csv" || migration_failed "Copying manifest.csv failed."
    fi

    if [ -n "$LEGACY_UPLOADS" ]; then
        step "Copying uploads ($(du -sh "$LEGACY_UPLOADS" | cut -f1))..."
        cp -R --preserve=mode,timestamps "$LEGACY_UPLOADS/." "$DATA_DIR/uploads/" \
            || migration_failed "Copying uploads failed."
        chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/uploads"
    fi

    chown -R "$SERVICE_USER:$SERVICE_USER" "$STAGING_DIR"
    chmod 600 "$STAGING_DIR/config.json"
    chmod 640 "$STAGING_DIR/rscp.db"
    local f
    for f in rscp.db config.json manifest.csv; do
        if [ -e "$STAGING_DIR/$f" ]; then
            mv -f "$STAGING_DIR/$f" "$DATA_DIR/$f"
        fi
    done
    rmdir "$STAGING_DIR" 2>/dev/null || true
    STAGING_DIR=""

    {
        echo "$MIGRATE_FROM ($LEGACY_REAL) on $(date -Iseconds)"
        echo "database=$LEGACY_DB"
        echo "config=$LEGACY_CONFIG"
        echo "manifest=${LEGACY_MANIFEST:-none}"
        echo "uploads=${LEGACY_UPLOADS:-none}"
        echo "legacy_unit=$LEGACY_UNIT file=${LEGACY_UNIT_FILE:-none} saved_as=${LEGACY_SAVED_UNIT:-none}"
    } >"$DATA_DIR/.migrated-from"
    chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/.migrated-from"
}

migration_failed() {
    warn "$1 Restarting the legacy service; its files were not modified."
    discard_migrated_data
    systemctl start "$LEGACY_UNIT.service" || true
    die "$1 Migration aborted; $LEGACY_UNIT is running again on the old layout."
}

# Moves (never deletes) data copied by a failed migration out of the way,
# so a corrected re-run starts clean from the untouched legacy data.
discard_migrated_data() {
    local f ts
    ts=$(date +%Y%m%d-%H%M%S)
    if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
        mv -f "$STAGING_DIR" "$DATA_DIR/.migrate-failed-$ts"
    fi
    STAGING_DIR=""
    for f in rscp.db rscp.db-wal rscp.db-shm config.json manifest.csv .migrated-from; do
        if [ -e "$DATA_DIR/$f" ]; then
            mv -f "$DATA_DIR/$f" "$DATA_DIR/$f.failed-migration-$ts"
        fi
    done
    return 0
}

# Takes the legacy unit out of service, right before the new units go in.
# Same name (rscp): the unit file is overwritten by install_units and its
# drop-ins (which could override User= or WorkingDirectory=) are removed.
# Other name: disabled, file left in place.
retire_legacy_unit() {
    if [ "$LEGACY_UNIT" = "$UNIT" ]; then
        if [ -d "$UNIT_DIR/$LEGACY_UNIT.service.d" ]; then
            mv -f "$UNIT_DIR/$LEGACY_UNIT.service.d" "$LEGACY_BACKUP_DIR/$LEGACY_UNIT.service.d.retired-$(date +%Y%m%d-%H%M%S)"
            info "Moved legacy drop-ins out of $UNIT_DIR (copy kept in $LEGACY_BACKUP_DIR)"
        fi
    else
        systemctl disable --quiet "$LEGACY_UNIT.service" 2>/dev/null || true
        info "Disabled legacy unit $LEGACY_UNIT"
    fi
    return 0
}

write_legacy_restore_script() {
    local script="$LEGACY_BACKUP_DIR/restore-legacy.sh"
    {
        echo "#!/bin/sh"
        echo "# Return to the 2.x install that --migrate-from replaced on $1."
        echo "# The legacy files under $LEGACY_REAL were never modified; anything"
        echo "# changed in the new install since then exists only in $DATA_DIR."
        echo "set -e"
        echo "systemctl stop $UNIT.service rscp-update.path 2>/dev/null || true"
        echo "systemctl disable $UNIT.service rscp-update.path 2>/dev/null || true"
        if [ "$LEGACY_UNIT" = "$UNIT" ] && [ "$LEGACY_UNIT_FILE" != "$UNIT_DIR/$UNIT.service" ]; then
            echo "mv -f '$UNIT_DIR/$UNIT.service' '$LEGACY_BACKUP_DIR/$UNIT.service.new-layout' 2>/dev/null || true"
        fi
        echo "cp -a '$LEGACY_SAVED_UNIT' '$LEGACY_UNIT_FILE'"
        if [ -n "$LEGACY_SAVED_DROPIN" ]; then
            echo "if [ ! -d '$UNIT_DIR/$LEGACY_UNIT.service.d' ]; then cp -a '$LEGACY_SAVED_DROPIN' '$UNIT_DIR/$LEGACY_UNIT.service.d'; fi"
        fi
        echo "systemctl daemon-reload"
        echo "systemctl enable $LEGACY_UNIT.service"
        echo "systemctl start $LEGACY_UNIT.service"
        echo "echo 'Legacy $LEGACY_UNIT restored and started.'"
    } >"$script"
    chmod 700 "$script"
}

restore_legacy() {
    warn "Returning to the legacy install."
    systemctl stop "$UNIT.service" 2>/dev/null || true
    if [ -n "${LEGACY_SAVED_UNIT:-}" ] && [ -x "$LEGACY_BACKUP_DIR/restore-legacy.sh" ]; then
        "$LEGACY_BACKUP_DIR/restore-legacy.sh" || warn "Restore script failed; see $LEGACY_BACKUP_DIR"
    else
        systemctl start "$LEGACY_UNIT.service" || true
    fi
    discard_migrated_data
}

# --------------------------------------------------------------------------
# Flows
# --------------------------------------------------------------------------

run_install() {
    check_os
    ensure_packages

    local old_release
    old_release=$(current_release)
    if [ "$UPGRADE" -eq 1 ] && [ -z "$old_release" ]; then
        die "--upgrade needs an existing install ($CURRENT not found). Run without --upgrade to install."
    fi
    if [ -n "$MIGRATE_FROM" ]; then
        check_migration
    else
        refuse_unmigrated_legacy
    fi

    choose_channel_interactively
    resolve_source
    log "Installing RSCP from $(describe_source)"

    if [ "$SRC_KIND" = "tag" ] && [ -n "$old_release" ] \
        && grep -q "\"ref\": \"$SRC_REF\"" "$old_release/.release-info" 2>/dev/null \
        && [ "${MIGRATE_NEEDED:-0}" -eq 0 ]; then
        log "Already running $SRC_REF; making sure the service is set up."
        ensure_user; ensure_data_dir; ensure_config; ensure_env_file
        install_units "$old_release"; enable_units
        systemctl start "$UNIT.service"
        write_status success "Already up to date (v$(release_version "$old_release"))." "" "$(release_version "$old_release")"
        return 0
    fi

    # 1. Build the new release completely while the old service keeps running.
    build_release

    # 2. Data: migrate a 2.x install, or back up the current database.
    ensure_user
    ensure_data_dir
    if [ -n "$MIGRATE_FROM" ] && [ "$MIGRATE_NEEDED" -eq 1 ]; then
        migrate_legacy_data
    elif [ -n "$old_release" ]; then
        backup_db "v$(release_version "$old_release")"
    fi
    ensure_config
    ensure_env_file

    # 3. Switch and restart; roll back automatically if unhealthy.
    local old_previous=""
    [ -L "$PREVIOUS" ] && old_previous=$(readlink -f "$PREVIOUS")
    step "Activating release $(basename "$NEW_RELEASE")..."
    activate_release "$NEW_RELEASE" "$old_release"
    if [ "$MIGRATE_NEEDED" -eq 1 ]; then
        retire_legacy_unit
    fi
    install_units "$NEW_RELEASE"
    enable_units

    if restart_and_check "$NEW_VERSION"; then
        prune_releases
        write_status success "Now running v$NEW_VERSION." "" "$NEW_VERSION"
        print_summary
        return 0
    fi

    # Unhealthy: put the links back the way they were and drop the failed
    # release, so a later "rollback" can never select it.
    if [ -n "$old_release" ]; then
        point_link "$old_release" "$CURRENT"
    else
        rm -f "$CURRENT"
    fi
    if [ -n "$old_previous" ]; then
        point_link "$old_previous" "$PREVIOUS"
    else
        rm -f "$PREVIOUS"
    fi
    case "$NEW_RELEASE" in "$RELEASES"/?*) rm -rf -- "$NEW_RELEASE" ;; esac

    if [ "$MIGRATE_NEEDED" -eq 1 ]; then
        restore_legacy
        die "The new install did not become healthy; the legacy service was restored. Nothing in $LEGACY_REAL was modified."
    fi
    if [ -n "$old_release" ]; then
        warn "v$NEW_VERSION is unhealthy; rolling back to $(basename "$old_release")."
        install_units "$old_release"
        systemctl restart "$UNIT.service" || true
        local old_version
        old_version=$(release_version "$old_release")
        if wait_healthy "$old_version"; then
            write_status rolled_back "" \
                "v$NEW_VERSION failed its health check; rolled back to v$old_version. Database backup kept at ${DB_BACKUP:-n/a}." \
                "$old_version"
        else
            write_status error "" "v$NEW_VERSION failed its health check and the rollback to v$old_version is not healthy either. Check: journalctl -u $UNIT"
        fi
        exit 1
    fi
    systemctl stop "$UNIT.service" 2>/dev/null || true
    die "RSCP did not become healthy. Check: journalctl -u $UNIT -n 100"
}

run_rollback() {
    local cur target
    cur=$(current_release)
    [ -n "$cur" ] || die "No RSCP install found at $CURRENT."
    target=""
    if [ -L "$PREVIOUS" ]; then
        target=$(readlink -f "$PREVIOUS")
    fi
    if [ -z "$target" ] || [ ! -d "$target" ] || [ "$target" = "$cur" ]; then
        target=$(ls -1dt "$RELEASES"/*/ 2>/dev/null | sed 's:/$::' | grep -vxF "$cur" | head -n 1 || true)
    fi
    [ -n "$target" ] && [ -d "$target" ] || die "No previous release to roll back to."

    local tver cver
    tver=$(release_version "$target")
    cver=$(release_version "$cur")
    step "Rolling back from $(basename "$cur") to $(basename "$target")..."
    activate_release "$target" "$cur"
    install_units "$target"
    if restart_and_check "$tver"; then
        write_status success "Rolled back to v$tver. The database was not changed." "" "$tver"
        log "Rolled back to $(basename "$target")."
        return 0
    fi
    warn "v$tver is unhealthy; returning to $(basename "$cur")."
    activate_release "$cur" "$target"
    install_units "$cur"
    systemctl restart "$UNIT.service" || true
    wait_healthy "$cver" || true
    write_status error "" "Rollback to v$tver failed its health check; stayed on v$cver."
    exit 1
}

print_summary() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo
    echo "${C_GREEN}RSCP v$NEW_VERSION is running.${C_OFF}"
    info "Code:     $CURRENT -> $NEW_RELEASE"
    info "Data:     $DATA_DIR"
    info "Logs:     journalctl -u $UNIT -f"
    info "Status:   systemctl status $UNIT"
    info "Open:     http://${ip:-<server-ip>}:$(health_url | sed -E 's|.*:([0-9]+)/healthz|\1|')"
    if [ -n "$MIGRATE_FROM" ] && [ "${MIGRATE_NEEDED:-0}" -eq 1 ]; then
        echo
        echo "${C_BLUE}Migrated from $MIGRATE_FROM:${C_OFF}"
        sed 's/^/    /' "$DATA_DIR/.migrated-from"
        info "Legacy unit file saved in $LEGACY_BACKUP_DIR; the legacy directories were not modified."
        info "To go back: $LEGACY_BACKUP_DIR/restore-legacy.sh"
    fi
    echo
}

main "$@"
