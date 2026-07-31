#!/bin/sh
set -eu

FCC_REPO="FiredMosquito831/free-claude-code"
FCC_LATEST_RELEASE_URL="https://api.github.com/repos/${FCC_REPO}/releases/latest"
PYTHON_VERSION="3.14.0"
MIN_UV_VERSION="0.11.0"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

# Resolved from the release feed at run time (or from --version).
FCC_VERSION=""
FCC_WHEEL_NAME=""
FCC_WHEEL_URL=""
FCC_WHEEL_SHA256=""

dry_run=0
requested_version=""
voice_nim=0
voice_local=0
voice_all=0
torch_backend=""
temporary_script=""
temporary_directory=""
release_wheel_path=""

show_usage() {
    cat <<'USAGE'
Usage: install.sh [options]

Installs or updates Free Claude Code to the latest published release.

Installs a compatible uv if one is missing. It does not install Claude Code,
Codex, or Pi -- install whichever of those you use yourself.

Options:
  --version VALUE          Install this exact release instead of the latest.
  --voice-nim              Install NVIDIA NIM voice transcription support.
  --voice-local            Install local Whisper voice transcription support.
  --voice-all              Install all voice transcription backends.
  --torch-backend VALUE    Use a uv PyTorch backend, such as cu130. Requires local voice.
  --dry-run                Print commands without running them.
  --help                   Show this help text.
USAGE
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

step() {
    printf '\n==> %s\n' "$1"
}

quote_arg() {
    case "$1" in
        *[!A-Za-z0-9_./:@%+=,-]*|"")
            escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
            printf '"%s"' "$escaped"
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}

print_command() {
    printf '+'
    for arg in "$@"; do
        printf ' '
        quote_arg "$arg"
    done
    printf '\n'
}

run() {
    print_command "$@"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi

    if "$@"; then
        return 0
    else
        status=$?
    fi

    fail "Command failed with exit code $status: $1"
}

cleanup() {
    if [ -n "$temporary_script" ] && [ -e "$temporary_script" ]; then
        rm -f "$temporary_script"
    fi
    if [ -n "$temporary_directory" ] && [ -d "$temporary_directory" ]; then
        rm -rf -- "$temporary_directory"
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

add_path_entry() {
    [ -n "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}

add_known_bin_directories() {
    if [ -n "${XDG_BIN_HOME:-}" ]; then
        add_path_entry "$XDG_BIN_HOME"
    fi

    if [ -n "${HOME:-}" ]; then
        add_path_entry "$HOME/.local/bin"
        add_path_entry "$HOME/.cargo/bin"
    fi

    export PATH
    hash -r 2>/dev/null || true
}

require_command() {
    if [ "$dry_run" -eq 0 ] && ! command -v "$1" >/dev/null 2>&1; then
        fail "$1 is required. Install it first, then rerun this installer."
    fi
}

download_and_run() {
    url=$1
    interpreter=$2
    label=$3
    non_interactive=${4:-0}

    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$url" -o "<temporary-script>"
        if [ "$non_interactive" -eq 1 ]; then
            printf '+ CODEX_NON_INTERACTIVE=1 '
            quote_arg "$interpreter"
            printf ' <temporary-script>\n'
        else
            print_command "$interpreter" "<temporary-script>"
        fi
        return 0
    fi

    temporary_script=$(mktemp "${TMPDIR:-/tmp}/fcc-install.XXXXXX") || fail "Unable to create a temporary file for $label."
    print_command curl -fsSL "$url" -o "$temporary_script"
    if curl -fsSL "$url" -o "$temporary_script"; then
        :
    else
        status=$?
        fail "Could not download the $label installer (curl exit code $status)."
    fi

    if [ ! -s "$temporary_script" ]; then
        fail "The downloaded $label installer was empty."
    fi

    if [ "$non_interactive" -eq 1 ]; then
        printf '+ CODEX_NON_INTERACTIVE=1 '
        quote_arg "$interpreter"
        printf ' '
        quote_arg "$temporary_script"
        printf '\n'
        if CODEX_NON_INTERACTIVE=1 "$interpreter" "$temporary_script"; then
            :
        else
            status=$?
            fail "$label installation failed with exit code $status."
        fi
    else
        print_command "$interpreter" "$temporary_script"
        if "$interpreter" "$temporary_script"; then
            :
        else
            status=$?
            fail "$label installation failed with exit code $status."
        fi
    fi

    rm -f "$temporary_script"
    temporary_script=""
}

verify_command() {
    command_name=$1
    display_name=$2

    if [ "$dry_run" -eq 1 ]; then
        print_command "$command_name" --version
        return 0
    fi

    command_path=$(command -v "$command_name" 2>/dev/null) || fail "$display_name was installed, but '$command_name' is not available on PATH."
    run "$command_path" --version
}

current_uv_version() {
    if output=$(uv --version); then
        :
    else
        return 1
    fi

    case "$output" in
        uv\ *) version=${output#uv } ;;
        *) version=$output ;;
    esac
    version=${version%% *}

    case "$version" in
        [0-9]*.[0-9]*.[0-9]*) printf '%s\n' "$version" ;;
        *) return 1 ;;
    esac
}

version_ge() {
    current=${1%%[-+]*}
    minimum=${2%%[-+]*}

    old_ifs=$IFS
    IFS=.
    set -- $current
    current_major=${1:-0}
    current_minor=${2:-0}
    current_patch=${3:-0}
    set -- $minimum
    minimum_major=${1:-0}
    minimum_minor=${2:-0}
    minimum_patch=${3:-0}
    IFS=$old_ifs

    case "$current_major$current_minor$current_patch$minimum_major$minimum_minor$minimum_patch" in
        *[!0-9]*) return 1 ;;
    esac

    [ "$current_major" -gt "$minimum_major" ] && return 0
    [ "$current_major" -lt "$minimum_major" ] && return 1
    [ "$current_minor" -gt "$minimum_minor" ] && return 0
    [ "$current_minor" -lt "$minimum_minor" ] && return 1
    [ "$current_patch" -ge "$minimum_patch" ]
}

verify_uv() {
    if [ "$dry_run" -eq 1 ]; then
        print_command uv --version
        return 0
    fi

    command -v uv >/dev/null 2>&1 || fail "uv was installed, but it is not available on PATH."
    version=$(current_uv_version) || fail "uv is present, but 'uv --version' did not return a valid version."
    if ! version_ge "$version" "$MIN_UV_VERSION"; then
        fail "uv $MIN_UV_VERSION or newer is required; found uv $version after installation."
    fi

    printf 'Verified uv %s.\n' "$version"
}

ensure_uv() {
    if [ "$dry_run" -eq 1 ]; then
        if command -v uv >/dev/null 2>&1; then
            print_command uv --version
            printf 'A compatible existing uv will be left unchanged; an obsolete one will be replaced by the standalone installer.\n'
        else
            printf 'uv is not installed; the current standalone uv would be installed.\n'
            download_and_run "$UV_INSTALL_URL" sh "uv"
            verify_uv
        fi
        return 0
    fi

    if command -v uv >/dev/null 2>&1; then
        version=$(current_uv_version) || fail "uv is present, but 'uv --version' did not return a valid version."
        if version_ge "$version" "$MIN_UV_VERSION"; then
            printf 'uv %s already satisfies >=%s; leaving it unchanged.\n' "$version" "$MIN_UV_VERSION"
            return 0
        fi
        printf 'uv %s is below %s; installing the current standalone uv.\n' "$version" "$MIN_UV_VERSION"
    else
        printf 'uv is not installed; installing the current standalone uv.\n'
    fi

    download_and_run "$UV_INSTALL_URL" sh "uv"
    add_known_bin_directories
    verify_uv
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --voice-nim)
                voice_nim=1
                ;;
            --voice-local)
                voice_local=1
                ;;
            --voice-all)
                voice_all=1
                ;;
            --torch-backend)
                shift
                [ "$#" -gt 0 ] || fail "--torch-backend requires a value."
                torch_backend=$1
                [ -n "$torch_backend" ] || fail "--torch-backend requires a non-empty value."
                ;;
            --torch-backend=*)
                torch_backend=${1#*=}
                [ -n "$torch_backend" ] || fail "--torch-backend requires a non-empty value."
                ;;
            --version)
                shift
                [ "$#" -gt 0 ] || fail "--version requires a value."
                requested_version=${1#v}
                [ -n "$requested_version" ] || fail "--version requires a value."
                ;;
            --version=*)
                requested_version=${1#*=}
                requested_version=${requested_version#v}
                [ -n "$requested_version" ] || fail "--version requires a value."
                ;;
            --dry-run)
                dry_run=1
                ;;
            --help|-h)
                show_usage
                exit 0
                ;;
            *)
                show_usage >&2
                fail "unknown option: $1"
                ;;
        esac
        shift
    done
}

validate_args() {
    include_local=$voice_local
    if [ "$voice_all" -eq 1 ]; then
        include_local=1
    fi

    if [ -n "$torch_backend" ] && [ "$include_local" -ne 1 ]; then
        fail "--torch-backend requires --voice-local or --voice-all."
    fi
}

resolve_release() {
    if [ -n "$requested_version" ]; then
        FCC_VERSION=$requested_version
    else
        # Read even during a dry run: it is a GET that changes nothing, and it
        # is the only way to report the version that would actually install.
        print_command curl -fsSL "$FCC_LATEST_RELEASE_URL"
        release_json=$(curl -fsSL -H "Accept: application/vnd.github+json" "$FCC_LATEST_RELEASE_URL" 2>/dev/null) ||
            fail "Could not reach the release feed to find the latest version."
        FCC_VERSION=$(printf '%s\n' "$release_json" |
            grep -m1 '"tag_name"' |
            sed -e 's/.*"tag_name"[[:space:]]*:[[:space:]]*"//' -e 's/".*//' -e 's/^v//')
        [ -n "$FCC_VERSION" ] ||
            fail "Could not read the latest release version from the release feed."
        # GitHub publishes a sha256 digest per asset, so the download is still
        # verified even though no checksum is pinned in this script.
        FCC_WHEEL_SHA256=$(printf '%s\n' "$release_json" |
            grep -m1 '"digest"' |
            sed -e 's/.*sha256://' -e 's/".*//')
    fi
    FCC_WHEEL_NAME="free_claude_code-${FCC_VERSION}-py3-none-any.whl"
    FCC_WHEEL_URL="https://github.com/${FCC_REPO}/releases/download/v${FCC_VERSION}/${FCC_WHEEL_NAME}"
}

download_verified_release_wheel() {
    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$FCC_WHEEL_URL" -o "<temporary-wheel>"
        if [ -n "$FCC_WHEEL_SHA256" ]; then
            printf '+ verify SHA-256 %s for <temporary-wheel>\n' "$FCC_WHEEL_SHA256"
        else
            printf '+ verify the SHA-256 published for this release\n'
        fi
        release_wheel_path="<verified-release-wheel>"
        return 0
    fi

    temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/fcc-wheel.XXXXXX") ||
        fail "Unable to create a temporary directory for the FCC release wheel."
    release_wheel_path="$temporary_directory/$FCC_WHEEL_NAME"
    print_command curl -fsSL "$FCC_WHEEL_URL" -o "$release_wheel_path"
    if ! curl -fsSL "$FCC_WHEEL_URL" -o "$release_wheel_path"; then
        fail "Could not download the FCC v$FCC_VERSION release wheel."
    fi
    [ -s "$release_wheel_path" ] ||
        fail "The downloaded FCC release wheel was empty."

    if command -v sha256sum >/dev/null 2>&1; then
        actual_sha256=$(sha256sum "$release_wheel_path")
    elif command -v shasum >/dev/null 2>&1; then
        actual_sha256=$(shasum -a 256 "$release_wheel_path")
    else
        fail "sha256sum or shasum is required to verify the FCC release wheel."
    fi
    actual_sha256=${actual_sha256%% *}
    [ "$actual_sha256" = "$FCC_WHEEL_SHA256" ] ||
        fail "FCC release wheel checksum mismatch; refusing to install."
    printf 'Verified FCC v%s release wheel SHA-256.\n' "$FCC_VERSION"
}

package_spec() {
    package_url=$1
    include_nim=$voice_nim
    include_local=$voice_local

    if [ "$voice_all" -eq 1 ]; then
        include_nim=1
        include_local=1
    fi

    if [ "$include_nim" -eq 1 ] && [ "$include_local" -eq 1 ]; then
        printf 'free-claude-code[voice,voice_local] @ %s' "$package_url"
    elif [ "$include_nim" -eq 1 ]; then
        printf 'free-claude-code[voice] @ %s' "$package_url"
    elif [ "$include_local" -eq 1 ]; then
        printf 'free-claude-code[voice_local] @ %s' "$package_url"
    else
        printf 'free-claude-code @ %s' "$package_url"
    fi
}

install_free_claude_code() {
    resolve_release
    download_verified_release_wheel
    package_url="file://$release_wheel_path"
    spec=$(package_spec "$package_url")

    if [ -n "$torch_backend" ]; then
        run uv tool install --force --refresh-package free-claude-code --python "$PYTHON_VERSION" --torch-backend "$torch_backend" "$spec"
    else
        run uv tool install --force --refresh-package free-claude-code --python "$PYTHON_VERSION" "$spec"
    fi
}

configure_and_verify_free_claude_code() {
    run uv tool update-shell

    if [ "$dry_run" -eq 1 ]; then
        print_command uv tool dir --bin
        printf '+ verify fcc-server, fcc-claude, fcc-codex, and fcc-pi in the uv tool bin directory\n'
        print_command fcc-server --version
        return 0
    fi

    print_command uv tool dir --bin
    if tool_bin=$(uv tool dir --bin); then
        :
    else
        status=$?
        fail "Could not determine the uv tool bin directory (exit code $status)."
    fi
    [ -n "$tool_bin" ] || fail "uv returned an empty tool bin directory."

    add_path_entry "$tool_bin"
    export PATH
    hash -r 2>/dev/null || true

    for command_name in fcc-server fcc-claude fcc-codex fcc-pi; do
        [ -x "$tool_bin/$command_name" ] || fail "Free Claude Code installation did not create $tool_bin/$command_name."
    done

    print_command "$tool_bin/fcc-server" --version
    if installed_version=$("$tool_bin/fcc-server" --version); then
        printf '%s\n' "$installed_version"
    else
        status=$?
        fail "Free Claude Code version verification failed with exit code $status."
    fi
    [ "$installed_version" = "free-claude-code $FCC_VERSION" ] ||
        fail "Expected free-claude-code $FCC_VERSION; found: $installed_version"
}

parse_args "$@"
validate_args
add_known_bin_directories

step "Checking installation prerequisites"
require_command curl
require_command bash
require_command sh
require_command mktemp

step "Ensuring uv $MIN_UV_VERSION or newer is installed"
ensure_uv

step "Installing or updating Free Claude Code"
install_free_claude_code

step "Configuring PATH and verifying Free Claude Code"
configure_and_verify_free_claude_code

if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
else
    printf '\nFree Claude Code %s is installed and verified.\n' "$FCC_VERSION"
    printf 'Start the proxy with: fcc-server\n'
    printf '\nIf you use Claude Code, Codex, or Pi, launch them through the proxy\n'
    printf 'with fcc-claude, fcc-codex, or fcc-pi.\n'
fi
