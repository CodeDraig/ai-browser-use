import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import _ipc as ipc
from . import paths


def _process_start_time(pid):
    """Opaque process-start-time fingerprint at PID, or None if unavailable.

    Two reads returning the same non-None value mean the PID still refers to
    the same process; a different value means the PID was reused. Used by
    restart_daemon() to keep the force-kill recovery path working even when
    the daemon has already torn down its IPC socket (e.g. during a slow
    remote shutdown), without falling back to "trust the pid file" — which
    would re-introduce the PID-reuse hazard.

    Linux:   /proc/<pid>/stat field 22 (starttime in clock ticks since boot).
    macOS:   `ps -o lstart= -p <pid>` (an absolute timestamp string).
    Windows: GetProcessTimes via ctypes (FILETIME creation time, 100-ns since 1601).
    Anywhere else: returns None; restart_daemon falls back to its strict
    identify-only check, which is safer than no check at all.
    """
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode("ascii", errors="replace")
        except (FileNotFoundError, PermissionError, OSError):
            return None
        # Field 2 is `(comm)`; comm can contain spaces and parens, so split off
        # everything after the LAST `)` and index from there.
        try:
            tail = raw[raw.rindex(")") + 2:].split()
            return tail[19]  # starttime is field 22 (0-indexed: 21 - skipped 2 = 19)
        except (ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stderr=subprocess.DEVNULL, timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        s = out.decode("ascii", errors="replace").strip()
        return s or None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
        except (OSError, AttributeError):
            return None
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_ft = wintypes.FILETIME()
            kernel_ft = wintypes.FILETIME()
            user_ft = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                h, ctypes.byref(creation), ctypes.byref(exit_ft),
                ctypes.byref(kernel_ft), ctypes.byref(user_ft),
            )
            if not ok:
                return None
            return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel32.CloseHandle(h)
    return None


def _load_env():
    repo_root = Path(__file__).resolve().parents[2]
    workspace = paths.workspace_dir()
    for p in (repo_root / ".env", workspace / ".env"):
        if not p.exists():
            continue
        _load_env_file(p)


def _load_env_file(p):
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

NAME = os.environ.get("BU_NAME", "default")
DOCTOR_TEXT_LIMIT = 140


def _log_tail(name):
    try:
        return ipc.log_path(name or NAME).read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    except (FileNotFoundError, IndexError, OSError):
        return None


def _needs_chrome_remote_debugging_prompt(msg):
    """True when Chrome needs the inspect-page permission flow."""
    lower = (msg or "").lower()
    return (
        "devtoolsactiveport not found" in lower
        or "enable chrome://inspect" in lower
        or "not live yet" in lower
        or (
            "ws handshake failed" in lower
            and (
                "403" in lower
                or "opening handshake" in lower
                or "timed out" in lower
                or "timeout" in lower
            )
        )
    )


def _needs_chrome_permission_popup(msg):
    """True when Chrome is reachable but waiting on the per-session Allow popup."""
    lower = (msg or "").lower()
    return "permission-blocked" in lower


def _chrome_not_running(msg):
    """True when the daemon found no running supported browser"""
    return "chrome-not-running" in (msg or "").lower()


def _is_local_chrome_mode(env=None):
    """True when the daemon discovers a local Chrome instead of a remote CDP WS."""
    env = env or {}
    return not (
        env.get("BU_CDP_WS")
        or env.get("BU_CDP_URL")
        or os.environ.get("BU_CDP_WS")
        or os.environ.get("BU_CDP_URL")
    )


def daemon_alive(name=None):
    # Ping handshake (not a bare connect) so a stale .port file + port reuse
    # after a daemon crash doesn't make us mistake an unrelated listener for ours.
    return ipc.ping(name or NAME, timeout=1.0)


def daemon_browser_kind(name=None):
    """'cdp' | 'local' as self-reported by a live daemon, else None.

    None covers unreachable daemons and pre-browser_kind daemons still running
    from an older version."""
    c = None
    try:
        c, token = ipc.connect(name or NAME, timeout=1.0)
        response = ipc.request(c, token, {"meta": "ping"})
        kind = response.get("browser_kind") if isinstance(response, dict) else None
        return kind if kind in {"cdp", "local"} else None
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, socket.timeout, OSError, ValueError):
        return None
    finally:
        if c:
            c.close()


def _daemon_endpoint_names():
    # BH_RUNTIME_DIR isolates one daemon per dir → no filename-prefix discovery,
    # just check whether our local endpoint exists. Without BH_RUNTIME_DIR, or
    # with BH_RUNTIME_DIR_SHARED=1, _RUNTIME is shared and we glob `bu-*.<suffix>`
    # to find every daemon in that runtime dir.
    suffix = ".port" if ipc.IS_WINDOWS else ".sock"
    if ipc.BH_RUNTIME_DIR and not ipc.BH_RUNTIME_DIR_SHARED:
        return [NAME] if (ipc._RUNTIME / f"bu{suffix}").exists() else []
    names = []
    for p in sorted(ipc._RUNTIME.glob(f"bu-*{suffix}")):
        raw = p.name[3:-len(suffix)]
        try:
            ipc._check(raw)
        except ValueError:
            continue
        names.append(raw)
    return names


def _daemon_browser_connection(name):
    c = None
    try:
        c, token = ipc.connect(name, timeout=1.0)
        response = ipc.request(c, token, {"meta": "connection_status"})
        if "error" in response:
            return None
        page = response.get("page")
        if page:
            page = {"title": page.get("title") or "(untitled)", "url": page.get("url") or ""}
        return {"name": name, "page": page}
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, socket.timeout, OSError, KeyError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if c:
            c.close()


def daemon_browser_ready(name=None):
    """Whether the selected daemon has a healthy attached browser connection."""
    return _daemon_browser_connection(name or NAME) is not None


def browser_connections():
    """Live browser-harness daemons with healthy CDP browser connections and their attached page."""
    out = []
    for name in _daemon_endpoint_names():
        conn = _daemon_browser_connection(name)
        if conn:
            out.append(conn)
    return out


def active_browser_connections():
    """Count live browser-harness daemons with a healthy CDP browser connection."""
    return len(browser_connections())


def _doctor_short_text(value, limit=None):
    limit = limit or DOCTOR_TEXT_LIMIT
    value = str(value)
    return value if len(value) <= limit else value[:limit - 3] + "..."


def _is_snap_browser(path: str) -> bool:
    """True when a Chrome binary path lives under /snap/ (Snap confinement on Linux)."""
    return bool(path) and "/snap/" in path.lower()


def _doctor_snap_probe_path(path: str) -> str:
    raw = str(path)
    try:
        resolved = os.path.realpath(raw)
    except OSError:
        resolved = raw
    return raw if _is_snap_browser(raw) else resolved


def _doctor_probe_chrome_binary_for_snap():
    """Return (label, probe_path) for the first Chrome/Chromium binary found, else (None, None).

    Honors BH_CHROME_PATH and CHROME_PATH before searching PATH for common names.
    """
    import shutil

    for key in ("BH_CHROME_PATH", "CHROME_PATH"):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                return (p.name, _doctor_snap_probe_path(str(p)))
        except OSError:
            continue
    for cmd in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
        w = shutil.which(cmd)
        if not w:
            continue
        try:
            return (cmd, _doctor_snap_probe_path(w))
        except OSError:
            continue
    return (None, None)


def _snap_linux_headless_doc_url():
    return "https://github.com/browser-use/browser-harness/blob/main/docs/snap-linux-headless.md"


def run_doctor_fix_snap():
    """Print steps to replace Snap Chromium with a native Chrome for CDP. Always exit 0."""
    doc = _snap_linux_headless_doc_url()
    print("browser-harness doctor --fix-snap")
    print()
    print("Snap-packaged Chromium cannot expose DevTools the way browser-harness needs.")
    print(f"Full background: {doc}")
    print()
    print("1. Install Google Chrome from Google's .deb (not the Snap store):")
    print("   wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb")
    print("   sudo apt install ./google-chrome-stable_current_amd64.deb")
    print()
    print("2. Point the harness (and your shell) at the native binary so PATH does not")
    print("   pick the Snap wrapper first. Example for bash (~/.bashrc or session env):")
    print("   export BH_CHROME_PATH=/usr/bin/google-chrome-stable")
    print("   # CHROME_PATH is also honored by doctor's snap probe if you prefer that name.")
    print()
    print("3. Launch Chrome from that path (Way 2) or open Chrome normally (Way 1),")
    print("   enable remote debugging per install.md, then verify:")
    print("   browser-harness --doctor")
    print()
    return 0


def ensure_daemon(wait=60.0, name=None, env=None):
    """Idempotent. Self-heals stale daemon, closed Chrome (launches it), cold
    Chrome, and missing Allow on chrome://inspect."""
    if daemon_alive(name):
        # Stale daemons accept connects AND reply to meta:* (pure Python) even when the
        # CDP WS to Chrome is dead — probe with a real CDP call and require "result".
        # Must go through ipc.connect so this works on Windows (TCP loopback) too;
        # raw AF_UNIX here would fail on every warm call and churn the daemon.
        for last in (False, True):
            try:
                s, token = ipc.connect(name or NAME, timeout=3.0)
                resp = ipc.request(s, token, {"method": "Target.getTargets", "params": {}})
                if "result" in resp: return
            except Exception:
                pass
            if not last: time.sleep(0.5)
        restart_daemon(name)

    import subprocess, sys
    local = _is_local_chrome_mode(env)
    launched_browser = False
    opened_inspect = False
    for _ in range(3):
        e = {**os.environ, **({"BU_NAME": name} if name else {}), **(env or {})}
        try:
            stderr_sink = open(ipc.log_path(name or NAME), "ab")
        except OSError:
            stderr_sink = subprocess.DEVNULL
        p = subprocess.Popen(
            [sys.executable, "-m", "browser_harness.daemon"],
            env=e, stdout=subprocess.DEVNULL, stderr=stderr_sink, **ipc.spawn_kwargs(),
        )
        if stderr_sink is not subprocess.DEVNULL:
            stderr_sink.close()
        spawned = time.time()
        deadline = spawned + wait
        hinted = not local
        while time.time() < deadline:
            if daemon_alive(name): return
            if p.poll() is not None: break
            if not hinted and time.time() - spawned > 2 and (_log_tail(name) or "").startswith("handshake-wait"):
                action = (
                    "run `browser-harness mac-approve` in another shell or click Allow"
                    if sys.platform == "darwin"
                    else "click Allow"
                )
                print(
                    f'browser-harness: Chrome is asking "Allow remote debugging?" — {action} to continue.',
                    file=sys.stderr,
                )
                hinted = True
            time.sleep(0.2)
        msg = _log_tail(name) or ""
        if local and msg.startswith("handshake-wait"):
            restart_daemon(name)
            raise RuntimeError(
                "permission-blocked: Chrome's Allow popup was not clicked in time -- wait for the user to click Allow, then retry."
            )
        if local and _needs_chrome_permission_popup(msg):
            print('browser-harness: Chrome is asking "Allow remote debugging?". Click Allow in Chrome, then retry browser work.', file=sys.stderr)
            restart_daemon(name)
            raise RuntimeError(
                "permission-blocked: wait for the user to click Allow in the Chrome permission popup before retrying."
            )
        if local and not launched_browser and _chrome_not_running(msg):
            # Chrome is closed — launch the browser and retry
            launched_browser = True
            restart_daemon(name)
            if not _launch_browser():
                raise RuntimeError(
                    "chrome-not-running: no supported browser is running and none could be launched -- ask the user to open Chrome, then retry."
                )
            print("browser-harness: Chrome isn't running — launching it. If Chrome shows an \"Allow remote debugging?\" popup, click Allow.", file=sys.stderr)
            from .daemon import supported_browser_running
            boot_deadline = time.time() + 15
            while time.time() < boot_deadline and not supported_browser_running():
                time.sleep(0.3)
            continue
        if local and not opened_inspect and _needs_chrome_remote_debugging_prompt(msg):
            opened_inspect = True
            from .daemon import remote_debugging_toggle_profiles, remote_debugging_user_enabled
            if remote_debugging_user_enabled():
                # chrome://inspect toggle is already on — connection died
                print('browser-harness: Chrome is asking "Allow remote debugging?". Click Allow in Chrome, then retry browser work.', file=sys.stderr)
                restart_daemon(name)
                raise RuntimeError(
                    "permission-blocked: wait for the user to click Allow in the Chrome permission popup before retrying."
                )
            restart_daemon(name)
            _open_chrome_inspect_once()
            if remote_debugging_toggle_profiles():
                # Toggle already ticked from a previous run, but Chrome 144+
                # wants new Allow for this browser run.
                todo = 'click Allow on Chrome\'s "Allow remote debugging?" popup (the checkbox is already ticked; if no popup appears, untick and re-tick it)'
            else:
                todo = 'tick "Allow remote debugging for this browser instance" and click Allow on the popup'
            raise RuntimeError(
                f"remote-debugging-setup: opened chrome://inspect/#remote-debugging in Chrome -- ask the user to {todo}. "
                "Warn them Chrome shows ONE more Allow popup when the harness connects on the next attempt (per-connection approval; it is expected, not a re-ask). "
                "Retry after the user confirms; do not retry before."
            )
        raise RuntimeError(msg or f"daemon {name or NAME} didn't come up -- check {ipc.log_path(name or NAME)}")


def restart_daemon(name=None):
    """Best-effort daemon shutdown + socket/pid cleanup.

    Name is historical: callers typically follow this with another
    `browser-harness` invocation, which auto-spawns a fresh daemon via
    ensure_daemon(). The function itself only stops.

    Identity is verified via ipc.identify() before any process signal, so
    a stale pid file whose number has been reused by an unrelated process
    is never SIGTERM'd. If the daemon is unreachable, we just clean up the
    pid file and socket and return — never escalate to a kill-by-pid-file.
    """
    import signal

    name = name or NAME
    pid_path = str(ipc.pid_path(name))

    # Two pieces of information are tracked separately:
    #   - daemon_pid: the daemon's self-reported PID, or None. Only daemons
    #     running this version (or newer) include `pid` in the ping response;
    #     pre-upgrade daemons return {pong: True} only and yield None here.
    #   - daemon_alive: whether ANY daemon answers ping. Keeps the shutdown
    #     IPC path working across upgrades — without it, a still-running
    #     pre-upgrade daemon would have its socket deleted out from under it
    #     while the process stayed alive.
    daemon_pid = ipc.identify(name, timeout=5.0)
    daemon_alive = daemon_pid is not None or ipc.ping(name, timeout=1.0)
    # Snapshot the daemon's process start-time as a secondary identity check.
    # The IPC socket can disappear before the process exits during shutdown,
    # so identify() going None partway through is not proof of process death.
    # Comparing start-time before SIGTERM lets us recover the original
    # force-kill behavior for slow shutdowns without re-opening the
    # PID-reuse hole — a reused PID would have a different start-time.
    daemon_start = _process_start_time(daemon_pid)

    if daemon_alive:
        try:
            c, token = ipc.connect(name, timeout=5.0)
            ipc.request(c, token, {"meta": "shutdown"})
            c.close()
        except Exception:
            pass

    if daemon_pid is not None:
        for _ in range(75):
            try:
                os.kill(daemon_pid, 0)
                time.sleep(0.2)
            except (ProcessLookupError, OSError, SystemError, OverflowError):
                break
        else:
            # Re-verify identity before escalating to SIGTERM. Two acceptable
            # signals, in priority order:
            #   1. ipc.identify() still returns the same PID — daemon's IPC is
            #      live, daemon is wedged. Safe to kill.
            #   2. start-time fingerprint of the original PID is unchanged —
            #      same process, just slow to exit.
            #      The IPC may already be gone; that's expected.
            # If neither holds, the PID may have been reused; skip SIGTERM.
            verified_pid = ipc.identify(name, timeout=1.0)
            same_process = verified_pid == daemon_pid or (
                daemon_start is not None
                and _process_start_time(daemon_pid) == daemon_start
            )
            if same_process:
                try:
                    os.kill(daemon_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError, SystemError, OverflowError):
                    pass

    ipc.cleanup_endpoint(name)
    try:
        os.unlink(pid_path)
    except FileNotFoundError:
        pass


def list_local_profiles():
    """Detected local browser profiles on this machine. Shells out to `profile-use list --json`."""
    import json, shutil, subprocess
    if not shutil.which("profile-use"):
        raise RuntimeError("profile-use not installed")
    return json.loads(subprocess.check_output(["profile-use", "list", "--json"], text=True, encoding="utf-8", errors="replace"))


def _version():
    """Version of the Browser Use distribution that bundles this harness."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("browser-use")
        except PackageNotFoundError:
            return ""
    except Exception:
        return ""


def _chrome_running():
    """Cross-platform best-effort check for a running Chromium-based browser."""
    import platform, subprocess
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.check_output(["tasklist"], text=True, errors="replace", timeout=5)
            names = ("chrome.exe", "msedge.exe", "helium.exe")
        else:
            out = subprocess.check_output(["ps", "-A", "-o", "comm="], text=True, errors="replace", timeout=5)
            names = ("Google Chrome", "chrome", "chromium", "Microsoft Edge", "msedge", "helium")
        return any(n.lower() in out.lower() for n in names)
    except Exception:
        return False


_BROWSER_LAUNCH = (
    # (profile-dir fragment, macOS app name, POSIX commands, Windows `start` target)
    ("chrome canary", "Google Chrome Canary", ("google-chrome-canary",), "chrome"),
    ("chromium", "Chromium", ("chromium", "chromium-browser"), "chromium"),
    ("chrome", "Google Chrome", ("google-chrome-stable", "google-chrome"), "chrome"),
    ("edge", "Microsoft Edge", ("microsoft-edge", "microsoft-edge-stable"), "msedge"),
    ("brave", "Brave Browser", ("brave-browser", "brave"), "brave"),
    ("arc", "Arc", (), None),
    ("dia", "Dia", (), None),
    ("comet", "Comet", (), None),
)
_DEFAULT_LAUNCH = (
    "Google Chrome",
    ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge"),
    "chrome",
)


def _browser_launch_spec(base):
    """(mac app, posix commands, windows target) for the browser w profile dir"""
    tail = "/".join(p.lower() for p in Path(base).parts[-2:])
    for frag, mac_app, posix_cmds, win_target in _BROWSER_LAUNCH:
        if frag in tail:
            return (mac_app, posix_cmds, win_target)
    return _DEFAULT_LAUNCH


def _profile_directory_args(base):
    """Relaunch skips Chrome's profile picker"""
    if not base:
        return []
    try:
        state = json.loads((Path(base) / "Local State").read_text(encoding="utf-8", errors="replace"))
        last = ((state.get("profile") or {}).get("last_used")) or "Default"
    except (OSError, ValueError, AttributeError):
        last = "Default"
    if not isinstance(last, str) or not (Path(base) / last).is_dir():
        return []
    return [f"--profile-directory={last}"]


def _launch_browser():
    """Prefers the browser whose profile already has perm box checked"""
    import platform, shutil, subprocess
    from .daemon import PROFILES, remote_debugging_toggle_profiles

    for key in ("BH_CHROME_PATH", "CHROME_PATH"):
        raw = (os.environ.get(key) or "").strip()
        if raw and Path(raw).expanduser().is_file():
            try:
                subprocess.Popen(
                    [str(Path(raw).expanduser())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **ipc.spawn_kwargs(),
                )
                return True
            except (OSError, subprocess.SubprocessError):
                # A path that exists but can't execute (permissions, wrong arch)
                # must fall through to normal discovery, not abort
                continue

    enabled = remote_debugging_toggle_profiles()
    base = enabled[0] if enabled else next((b for b in PROFILES if (b / "Local State").exists()), None)
    mac_app, posix_cmds, win_target = _browser_launch_spec(base) if base else _DEFAULT_LAUNCH
    profile_args = _profile_directory_args(base)
    try:
        system = platform.system()
        if system == "Darwin":
            cmd = ["open", "-a", mac_app] + (["--args"] + profile_args if profile_args else [])
            r = subprocess.run(cmd, timeout=10, check=False, capture_output=True)
            if r.returncode != 0 and mac_app != "Google Chrome":
                # Different app → its profile dir may not match; launch plain
                r = subprocess.run(["open", "-a", "Google Chrome"], timeout=10, check=False, capture_output=True)
            return r.returncode == 0
        if system == "Windows":
            # `start <name>` resolves browsers via App Paths without knowing the install dir
            subprocess.Popen(["cmd", "/c", "start", "", win_target or "chrome"] + profile_args, **ipc.spawn_kwargs())
            return True
        for cmd in posix_cmds or _DEFAULT_LAUNCH[1]:
            w = shutil.which(cmd)
            if w:
                subprocess.Popen([w] + profile_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **ipc.spawn_kwargs())
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def _open_chrome_inspect():
    """Open chrome://inspect/#remote-debugging so the user can tick the checkbox."""
    import platform, subprocess, webbrowser
    url = "chrome://inspect/#remote-debugging"
    if platform.system() == "Darwin":
        try:
            r = subprocess.run([
                "osascript",
                "-e", 'tell application "Google Chrome" to activate',
                "-e", f'tell application "Google Chrome" to open location "{url}"',
            ], timeout=5, check=False, capture_output=True)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


INSPECT_REOPEN_TTL = 180.0  # seconds open new chrome://inspect tab


def _open_chrome_inspect_once():
    """Open chrome://inspect at most once per INSPECT_REOPEN_TTL across invocations"""
    marker = paths.inspect_marker()
    try:
        if time.time() - marker.stat().st_mtime < INSPECT_REOPEN_TTL:
            return
    except OSError:
        pass
    if not _open_chrome_inspect():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


def run_doctor():
    """Read-only diagnostics. Exit 0 iff everything looks healthy."""
    import platform, sys
    cur = _version()
    chrome = _chrome_running()
    daemon = daemon_alive()
    connections = browser_connections()
    cur_display = cur or "(unknown)"
    doc_url = _snap_linux_headless_doc_url()

    def row(label, ok, detail=""):
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")

    print("browser-harness doctor")
    print(f"  platform          {platform.system()} {platform.release()}")
    print(f"  python            {sys.version.split()[0]}")
    print(f"  version           {cur_display} (bundled)")
    if platform.system() == "Linux":
        bname, bpath = _doctor_probe_chrome_binary_for_snap()
        if bname and bpath and _is_snap_browser(bpath):
            print("[snap-detect]")
            print(f"Browser: {bname} (snap) — WARNING: Snap confinement prevents CDP binding.")
            print(f"  Fix: Install Chrome natively (see docs/snap-linux-headless.md)")
            print(f"  Docs: {doc_url}")
    row("chrome running", chrome, "" if chrome else "start chrome/edge")
    row("daemon alive", daemon, "" if daemon else "see install.md")
    row("active browser connections", bool(connections), str(len(connections)))
    for conn in connections:
        page = conn.get("page")
        if page:
            title = _doctor_short_text(page["title"])
            url = _doctor_short_text(page["url"])
            print(f"        {conn['name']} — active page: {title} — {url}")
        else:
            print(f"        {conn['name']} — active page: (no real page)")
    # Core health = chrome + daemon.
    return 0 if (chrome and daemon) else 1
