import os
import sys
import time

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

from . import recorder
from .admin import (
    _version,
    ensure_daemon,
    list_local_profiles,  # noqa: F401 - exposed to executed CLI programs
    restart_daemon,
    run_doctor,
    run_doctor_fix_snap,
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
  browser-harness skill               print the browser-harness skill text
  browser-harness recordings          show recording status and recent sessions
  browser-harness recordings --latest   print the newest recording directory
  browser-harness recordings enable   save browser actions locally by default
  browser-harness recordings disable  stop saving browser actions by default
  browser-harness video init <recording>      prepare a recording for editing
  browser-harness video review <recording>    compile and review the video
  browser-harness video export <recording> --reviewed   export a verified MP4
  browser-harness --reload         stop the daemon so next call picks up code changes
"""

USAGE = """Usage:
  browser-harness <<'PY'
  print(page_info())
  PY
"""


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
    try:
        ensure_daemon()
    except RuntimeError as e:
        print(f"browser-harness: {e}", file=sys.stderr)
        sys.exit(1)
    _install_helper_trace()
    exec(code, globals())


if __name__ == "__main__":
    main()
