"""Read/write JSON files in the GitHub repo (the bot's memory) and start the video workflow."""

import base64
import json
import os

import requests
import urllib3.util.connection as _conn

_conn.HAS_IPV6 = False  # IPv4 only (IPv6 can hang on Render)

API = "https://api.github.com"


def _repo() -> str:
    return os.getenv("GH_REPO", "sharath11yr/gptk750m")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['GH_DISPATCH_TOKEN']}",
            "Accept": "application/vnd.github+json"}


def read(path: str, default):
    """Returns (data, sha)."""
    r = requests.get(f"{API}/repos/{_repo()}/contents/{path}", headers=_headers(), timeout=30,
                     params={"ref": os.getenv("GH_BRANCH", "main")})
    if r.status_code == 404:
        return default, None
    r.raise_for_status()
    body = r.json()
    return json.loads(base64.b64decode(body["content"]).decode()), body["sha"]


def write(path: str, data, sha: str | None, message: str) -> str:
    content = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=1).encode()).decode()
    body = {"message": message, "content": content, "branch": os.getenv("GH_BRANCH", "main")}
    if sha:
        body["sha"] = sha
    r = requests.put(f"{API}/repos/{_repo()}/contents/{path}", headers=_headers(), json=body,
                     timeout=30)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def update(path: str, default, change, message: str):
    """Read, apply change(data), write; retries if someone else wrote meanwhile."""
    for _ in range(4):
        data, sha = read(path, default)
        result = change(data)
        try:
            write(path, data, sha, message)
            return result
        except requests.HTTPError as exc:
            if exc.response.status_code not in (409, 422):
                raise
    raise RuntimeError(f"Could not save {path}")


def dispatch(workflow: str, inputs: dict) -> None:
    r = requests.post(f"{API}/repos/{_repo()}/actions/workflows/{workflow}/dispatches",
                      headers=_headers(), timeout=30,
                      json={"ref": os.getenv("GH_BRANCH", "main"), "inputs": inputs})
    r.raise_for_status()
