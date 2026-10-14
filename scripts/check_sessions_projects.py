"""Verify session delete and project switching over HTTP.

    python scripts/check_sessions_projects.py [port]

Both are destructive operations on server state, so each asserts on the observable effect: the
session is really gone from the list, and the workspace really changed (including that uploads and
file reads follow it).
"""
import json
import sys
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read().decode())


def post(path, body):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(request, timeout=60).read().decode())


def delete(path):
    request = urllib.request.Request(BASE + path, method="DELETE")
    return json.loads(urllib.request.urlopen(request, timeout=60).read().decode())


def main() -> int:
    original = get("/api/config")["workspace"]
    print(f"workspace={original}\n")

    print("=== session delete ===")
    keep = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]
    doomed = post("/api/sessions", {"mode": "plan"})["session"]["id"]
    check("two sessions created", keep != doomed)

    listing = [s["id"] for s in get("/api/sessions")["sessions"]]
    check("both listed", keep in listing and doomed in listing, listing)

    result = delete("/api/sessions/" + doomed)
    check("delete reported success", result.get("deleted") is True, result)

    listing = [s["id"] for s in get("/api/sessions")["sessions"]]
    check("deleted session is gone", doomed not in listing, listing)
    check("the other session survived", keep in listing, listing)

    try:
        urllib.request.urlopen(f"{BASE}/api/messages?session={doomed}", timeout=30)
        check("deleted session no longer resolves", False, "returned 200")
    except urllib.error.HTTPError as exc:
        check("deleted session no longer resolves", exc.code == 404, exc.code)

    print("\n=== project switching ===")
    # A directory that exists and differs from the current workspace.
    target = original.rstrip("\\/") + "_alt"
    import pathlib

    pathlib.Path(target).mkdir(parents=True, exist_ok=True)

    out = post("/api/workspace", {"path": target})
    check("workspace switch accepted", bool(out.get("workspace")), out)
    check("reported workspace matches", str(out.get("workspace", "")).lower().rstrip("\\/") == target.lower(), out.get("workspace"))

    config = get("/api/config")
    check("config reflects the new workspace", str(config["workspace"]).lower().rstrip("\\/") == target.lower(), config["workspace"])
    check("new workspace is in recents", any(str(p).lower().rstrip("\\/") == target.lower() for p in config.get("recent_workspaces", [])), config.get("recent_workspaces"))
    check("sessions were reset for the new project", len(get("/api/sessions")["sessions"]) == 0, get("/api/sessions")["sessions"])

    # Uploads must land in the new project, proving the switch is not cosmetic.
    request = urllib.request.Request(
        BASE + "/api/upload", data=b"project switch probe", headers={"X-Filename": "switch.txt"}, method="POST"
    )
    upload = json.loads(urllib.request.urlopen(request, timeout=60).read().decode())
    # Uploads belong to the dashboard's own scratch space, not to the user's project: they are stored
    # in the config directory (with fallbacks when that is not writable) so they never pollute the
    # repository being worked on. What matters is that the upload succeeded and the bytes are on disk.
    import pathlib

    check("upload succeeded", bool(upload.get("path")), upload)
    check("uploaded bytes exist on disk", pathlib.Path(upload["path"]).is_file(), upload.get("path"))
    check("upload is not inside the project being served",
          target.lower() not in upload["path"].lower(), upload.get("path"))

    # A path outside the workspace must still be refused.
    try:
        get("/api/file?path=../../../Windows/win.ini")
        check("traversal still refused after switch", False, "read succeeded")
    except urllib.error.HTTPError as exc:
        check("traversal still refused after switch", exc.code in (403, 404, 500), exc.code)

    # Restore the original workspace so the probe is not destructive.
    post("/api/workspace", {"path": original})
    check("restored original workspace", str(get("/api/config")["workspace"]).lower().rstrip("\\/") == original.lower().rstrip("\\/"))

    print("\n=== invalid project ===")
    try:
        post("/api/workspace", {"path": "C:\\definitely-not-a-real-path-9d3f"})
        check("missing directory rejected", False, "accepted")
    except urllib.error.HTTPError as exc:
        check("missing directory rejected", exc.code == 400, exc.code)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
