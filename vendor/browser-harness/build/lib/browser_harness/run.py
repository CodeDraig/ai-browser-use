import os
import sys
import time
import urllib.request

# Windows default stdout/stderr encoding is cp1252
# which can't encode the 🐴 marker helpers prepend to tab titles (or anything
# else outside the locale charset). Force UTF-8 so `print(page_info())` and
# tracebacks carrying page titles don't UnicodeEncodeError on Windows. #124(4).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from . import auth, recorder
from .admin import (
    NAME,
    _version,
    daemon_alive,
    ensure_daemon,
    list_cloud_profiles,  # noqa: F401 - exposed to executed CLI programs
    list_local_profiles,  # noqa: F401 - exposed to executed CLI programs
    print_update_banner,
    restart_daemon,
    run_doctor,
    run_doctor_fix_snap,
    run_update,
    start_remote_daemon,
    stop_remote_daemon,  # noqa: F401 - exposed to executed CLI programs
    sync_local_profile,  # noqa: F401 - exposed to executed CLI programs
)
from .helpers import *  # noqa: F403 - CLI helpers are intentionally pre-imported

HELP = """Browser Harness

Read SKILL.md for the default workflow and examples.

Typical usage:
  browser-harness <<'PY'
  ensure_real_tab()
  print(page_info())
  PY

Helpers are pre-imported. The daemon auto-starts and connects to the running browser.

Commands:
  browser-harness --version        print the installed version
  browser-harness --doctor         diagnose install, daemon, and browser state
  browser-harness doctor           same as --doctor
  browser-harness doctor --fix-snap   print how to fix Snap Chromium blocking CDP (Linux)
  browser-harness mac-approve         approve Chrome's macOS remote debugging sheet
  browser-harness auth login          sign in to Browser Use Cloud for cloud browsers
  browser-harness auth login --device-code   sign in from SSH/headless environments
  browser-harness auth status         show Browser Use Cloud auth state
  browser-harness auth logout         remove stored Browser Use Cloud auth
  browser-harness skill               print the browser-harness skill text
  browser-harness recordings          show recording status and recent sessions
  browser-harness recordings --latest   print the newest recording directory
  browser-harness recordings enable   save browser actions locally by default
  browser-harness recordings disable  stop saving browser actions by default
  browser-harness video init <recording>      prepare a recording for editing
  browser-harness video review <recording>    compile and review the video
  browser-harness video export <recording> --reviewed   export a verified MP4
  browser-harness --update [-y]    pull the latest version (agents: pass -y)
  browser-harness --reload         stop the daemon so next call picks up code changes
"""

USAGE = """Usage:
  browser-harness <<'PY'
  print(page_info())
  PY
"""


# Probe /json/version (not a bare TCP connect) so a non-Chrome process bound to
# 9222/9223 doesn't masquerade as Chrome and skip the cloud bootstrap. Mirrors
# daemon.py's fallback probe.
def _local_chrome_listening():
    for port in (9222, 9223):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3).close()
            return True
        except OSError:
            pass
    return False


# BU_CDP_URL / BU_CDP_WS are documented to override local Chrome discovery
# (install.md:58-59), so they must also block cloud auto-bootstrap. Without this
# guard, start_remote_daemon() in admin.py overwrites BU_CDP_WS in the daemon
# env with a cloud WebSocket URL, silently replacing the user's explicit endpoint
# *and* billing them for a cloud browser they never asked for.
def _explicit_cdp_configured():
    return bool(os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS"))


def _cloud_auth_configured():
    try:
        auth.get_browser_use_api_key()
        return True
    except (auth.CloudAuthRequired, auth.AuthError, OSError):
        return False


def _print_skill():
    from importlib import resources
    # SKILL.md is UTF-8 (contains emoji); locale-codec read crashes on gbk Windows
    print(resources.files("browser_harness").joinpath("SKILL.md").read_text(encoding="utf-8"), end="")


def _traced(name, fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        step_start = time.monotonic()
        result = fn(*args, **kwargs)
        recorder.observe(name, args, kwargs, round(time.monotonic() - step_start, 3))
        return result

    wrapper.__bh_traced__ = True
    return wrapper


def _install_helper_trace():
    from . import helpers

    g = globals()
    for name in dir(helpers):
        if name.startswith("_"):
            continue
        fn = g.get(name)
        if callable(fn) and not isinstance(fn, type) and not getattr(fn, "__bh_traced__", False):
            g[name] = _traced(name, fn)


def main():
    _run(sys.argv[1:])


def _run(args):
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] == "--version":
        print(_version() or "unknown")
        return
    if args and args[0] == "--doctor":
        sys.exit(run_doctor())
    if args and args[0] == "doctor":
        rest = args[1:]
        if rest == ["--fix-snap"]:
            sys.exit(run_doctor_fix_snap())
        if rest:
            print("usage: browser-harness doctor [--fix-snap]", file=sys.stderr)
            sys.exit(2)
        sys.exit(run_doctor())
    if args and args[0] == "auth":
        sys.exit(auth.run_auth_cli(args[1:]))
    if args and args[0] == "mac-approve":
        from . import macos

        sys.exit(macos.run_cli(args[1:]))
    if args and args[0] == "skill":
        if len(args) != 1:
            print("usage: browser-harness skill", file=sys.stderr)
            sys.exit(2)
        _print_skill()
        return
    if args and args[0] == "recordings":
        rest = args[1:]
        if rest == ["--latest"]:
            latest = recorder.latest_recording()
            if latest is None:
                print("no recordings found", file=sys.stderr)
                sys.exit(1)
            print(latest)
            return
        if rest in (["enable"], ["disable"]):
            enabled = rest == ["enable"]
            recorder.set_auto_recording(enabled)
            print(f"auto-recording preference {'enabled' if enabled else 'disabled'}")
            return
        if rest:
            print("usage: browser-harness recordings [--latest|enable|disable]", file=sys.stderr)
            sys.exit(2)
        enabled, source = recorder.auto_recording_setting()
        print(f"auto-recording: {'on' if enabled else 'off'} ({source})")
        active = recorder.recording_dir()
        print(f"active: {active or 'none'}")
        recent = recorder.recordings()
        print(f"latest: {recent[0] if recent else 'none'}")
        return
    if args and args[0] == "video":
        from . import video

        sys.exit(video.run_cli(args[1:]))
    if args and args[0] == "--update":
        yes = any(a in {"-y", "--yes"} for a in args[1:])
        sys.exit(run_update(yes=yes))
    if args and args[0] == "--reload":
        restart_daemon()
        print("daemon stopped — will restart fresh on next call")
        return
    if args and args[0] == "--debug-clicks":
        os.environ["BH_DEBUG_CLICKS"] = "1"
        args = args[1:]
    if not args and not sys.stdin.isatty():
        code = sys.stdin.read()
        if not code.strip():
            sys.exit(USAGE)
    else:
        sys.exit(USAGE)
    print_update_banner()
    # Auto-bootstrap a cloud browser is opt-in via BU_AUTOSPAWN — BROWSER_USE_API_KEY alone
    # is not enough, since the key is commonly set for unrelated reasons (profile sync,
    # cloud API calls, parent agents managing their own session). An explicit BU_CDP_URL
    # or BU_CDP_WS also blocks the spawn so we honour the precedence install.md promises.
    cloud_admin = code.lstrip().startswith(("start_remote_daemon(", "stop_remote_daemon("))
    if not cloud_admin:
        if (
            not daemon_alive()
            and not _local_chrome_listening()
            and not _explicit_cdp_configured()
            and _cloud_auth_configured()
            and os.environ.get("BU_AUTOSPAWN")
        ):
            start_remote_daemon(NAME)
        try:
            ensure_daemon()
        except RuntimeError as e:
            # Setup/permission errors are instructions for calling agent
            print(f"browser-harness: {e}", file=sys.stderr)
            sys.exit(1)
    _install_helper_trace()
    exec(code, globals())


if __name__ == "__main__":
    main()
