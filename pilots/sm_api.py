"""SpaceMolt v2 API helper — generic, credentials from environment.

SPACEMOLT_USER / SPACEMOLT_PASS env vars required.
Same shape as the agent's private helper; safe to share.
"""
import json, os, time, urllib.request, urllib.error

BASE = "https://game.spacemolt.com/api/v2"
USERNAME = os.environ["SPACEMOLT_USER"]
PASSWORD = os.environ["SPACEMOLT_PASS"]

SID = None
LAST_MUTATION = 0.0
MUTATION_INTERVAL = 11.0  # 1 mutation per 10s tick

class APIError(Exception):
    pass

def _post(path, body=None, headers=None, timeout=30):
    url = f"{BASE}/{path}"
    data = json.dumps(body or {}).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"raw": str(e)}
        raise APIError(f"HTTP {e.code} on {path}: {json.dumps(err)[:500]}")
    except Exception as e:
        raise APIError(f"network error on {path}: {e}")

def new_session():
    global SID
    r = _post("session")
    SID = r["session"]["id"]
    return SID

def login():
    return _post("spacemolt_auth/login", {"username": USERNAME, "password": PASSWORD},
                {"X-Session-Id": SID}).get("result", {})

def ensure_auth():
    new_session()
    msg = login()
    print(f"[auth] {str(msg)[:120]}", flush=True)
    return msg

def _maybe_wait():
    wait = LAST_MUTATION + MUTATION_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)

def call(tool, action, body=None, mutating=True, max_retries=2, timeout=90):
    global LAST_MUTATION
    path = f"{tool}/{action}"
    last_err = None
    for _ in range(max_retries + 1):
        if mutating:
            _maybe_wait()
        try:
            return _post(path, body or {}, {"X-Session-Id": SID}, timeout=timeout)
        except APIError as e:
            last_err = e
            msg = str(e)
            if "429" in msg:
                time.sleep(12)
                continue
            if "401" in msg:
                ensure_auth()
                continue
            raise
    raise APIError(f"failed after {max_retries} retries: {last_err}")

def mutate(tool, action, body=None, timeout=90):
    r = call(tool, action, body, mutating=True, timeout=timeout)
    global LAST_MUTATION
    LAST_MUTATION = time.time()
    return r

def query(tool, action, body=None):
    return call(tool, action, body, mutating=False)

def structured(r):
    """Pull structuredContent from a response, falling back to parsed result."""
    if isinstance(r, dict):
        if r.get("structuredContent"):
            return r["structuredContent"]
        res = r.get("result")
        if isinstance(res, str):
            try:
                return json.loads(res)
            except Exception:
                return res
        return res
    return r
