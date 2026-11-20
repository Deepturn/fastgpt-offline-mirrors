"""Internal console and isolated verification worker."""
import base64
import hashlib
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

from bundle import MAX_UPLOAD, digest, inspect_bundle, read, require, verify
from registry import (NPM_URL, PYPI_URL, catalog, endpoint, json_atomic, npm_index,
                      publish_npm, publish_wheels, wheel_index)

STATE = Path(os.environ.get("MIRROR_STATE", "/state"))
SECRETS = Path(os.environ.get("MIRROR_SECRETS", "/secrets"))
JOBS = STATE / "jobs"
PUBLISH = threading.Lock()
STATIC = Path(__file__).parent / "static"
ID = re.compile("[0-9a-f]{32}")
MAX_JOBS = int(os.environ.get("MAX_STAGED_JOBS", 12))


def credentials():
    return read(SECRETS / "manager.json")


def make_credentials():
    password = sys.stdin.readline().rstrip("\n")
    require(len(password) >= 16, "Use a password of at least 16 characters")
    salt = secrets.token_hex(24)
    print(json.dumps({"salt": salt, "hash": password_hash(password, salt),
                      "session_key": secrets.token_hex(32)}))


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


def login_ticket():
    now = int(time.time())
    value = f"{now}.{secrets.token_hex(12)}"
    key = bytes.fromhex(credentials()["session_key"])
    return value + "." + hmac.new(key, value.encode(), "sha256").hexdigest()


def valid_ticket(ticket):
    if not re.fullmatch(r"\d{10}\.[0-9a-f]{24}\.[0-9a-f]{64}", ticket):
        return False
    value, sig = ticket.rsplit(".", 1)
    key = bytes.fromhex(credentials()["session_key"])
    return (0 <= time.time() - int(value.split(".", 1)[0]) <= 3600 and
            hmac.compare_digest(sig, hmac.new(key, value.encode(), "sha256").hexdigest()))


def job_data(job):
    result = read(job / "upload.json")
    for name in ("verify", "publish"):
        file = job / f"{name}.json"
        if file.is_file():
            result.update(read(file))
    return result


def worker():
    while True:
        for job in sorted(JOBS.iterdir()):
            if not ID.fullmatch(job.name) or not (job / "upload.json").is_file() or (job / "verify.json").exists():
                continue
            directory = job / "bundle"
            try:
                shutil.rmtree(directory, ignore_errors=True)
                manifest, files = inspect_bundle(job / "upload.tar.gz", directory)
                verify(directory, manifest, files)
                result = {"status": "verified", "kind": manifest["kind"], "profile": manifest["profile"],
                          "files": files, "checksums": manifest["sha256"]}
            except Exception as exc:
                shutil.rmtree(directory, ignore_errors=True)
                (job / "upload.tar.gz").unlink(missing_ok=True)
                result = {"status": "rejected", "error": str(exc)[:10000]}
            json_atomic(job / "verify.json", result)
        time.sleep(1)


def preview(job, data):
    index = wheel_index() if data["kind"] == "pypi" else npm_index()
    items = []
    for rel, package in data["files"].items():
        key = Path(rel).name if data["kind"] == "pypi" else (package["name"], package["version"])
        existing = index.get(key)
        status = "new"
        if existing:
            status = "existing" if digest(existing) == data["checksums"][rel] else "conflict"
        items.append({"file": rel, **package, "action": status})
    return items


def publish(job):
    with PUBLISH:
        data = job_data(job)
        require(data["status"] in {"verified", "partial", "publishing"}, "The bundle is not ready for publication")
        statuses = preview(job, data)
        if any(row["action"] == "conflict" for row in statuses):
            json_atomic(job / "publish.json", {"status": "partial", "published": data.get("published", {}),
                                              "error": "A matching package/version has different bytes"})
            return
        result = {"status": "publishing", "published": data.get("published", {})}
        json_atomic(job / "publish.json", result)
        source = job / "bundle"
        try:
            for row in statuses:
                rel = row["file"]
                require(digest(source / rel) == data["checksums"][rel], f"Changed verified file: {rel}")
            if data["kind"] == "pypi":
                publish_wheels(job.name, [row["file"] for row in statuses if row["action"] == "new"], source)
                for row in statuses:
                    result["published"].setdefault(row["file"], "published" if row["action"] == "new" else "existing")
            else:
                password = (SECRETS / "publisher-password").read_text().strip()
                auth = "Basic " + base64.b64encode(f"mirror-publisher:{password}".encode()).decode()
                for row in statuses:
                    rel = row["file"]
                    status = publish_npm(source / rel, row["name"], row["version"], data["checksums"][rel], auth)
                    result["published"].setdefault(rel, status)
                    json_atomic(job / "publish.json", result)
            result.update(status="published", published_at=time.time())
        except Exception as exc:
            result.update(status="partial", error=str(exc)[:10000])
        json_atomic(job / "publish.json", result)
        if result["status"] == "published":
            shutil.rmtree(source, ignore_errors=True)
            (job / "upload.tar.gz").unlink(missing_ok=True)
        return job_data(job)


class Handler(BaseHTTPRequestHandler):
    routes = {"/": ("index.html", "text/html; charset=utf-8"),
              "/app.js": ("app.js", "application/javascript; charset=utf-8"),
              "/style.css": ("style.css", "text/css; charset=utf-8"),
              "/guide.md": ("guide.md", "text/markdown; charset=utf-8"),
              "/mirror-bundle": ("mirror-bundle", "text/plain; charset=utf-8")}
    login_lock = threading.Lock()
    attempts = {}
    upload_lock = threading.Lock()

    def output(self, status, body, mime="application/json; charset=utf-8", headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def admin(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        return "mirror_session" in jar and valid_ticket(jar["mirror_session"].value)

    def get_job(self, name):
        require(ID.fullmatch(name) is not None, "Invalid job ID")
        job = JOBS / name
        require((job / "upload.json").is_file(), "Job not found")
        return job

    def do_GET(self):
        try:
            route = urlsplit(self.path).path
            if route in self.routes:
                file, mime = self.routes[route]
                headers = {"Content-Disposition": f'attachment; filename="{file}"'} if route == "/mirror-bundle" else None
                return self.output(200, (STATIC / file).read_bytes(), mime, headers)
            if route == "/health":
                return self.output(200, {"status": "ready"})
            if route == "/api/catalog":
                return self.output(200, {"packages": catalog(), "admin": self.admin(),
                                        "sources": {"pypi": {"url": os.environ["PUBLIC_PYPI"], "healthy": endpoint(PYPI_URL, "/simple/")},
                                                    "npm": {"url": os.environ["PUBLIC_NPM"], "healthy": endpoint(NPM_URL, "/-/ping")}}})
            if not self.admin():
                return self.output(401, {"error": "Sign in to manage imports"})
            if route == "/api/jobs":
                return self.output(200, [job_data(job) for job in sorted(
                    JOBS.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)
                    if (job / "upload.json").is_file()][:100])
            if route.startswith("/api/jobs/"):
                job_id = route.removeprefix("/api/jobs/").removesuffix("/report")
                job = self.get_job(job_id)
                data = job_data(job)
                if data["status"] in {"verified", "partial", "publishing"}:
                    data["preview"] = preview(job, data)
                headers = {"Content-Disposition": f'attachment; filename="mirror-{job_id}.json"'} if route.endswith("/report") else None
                return self.output(200, data, headers=headers)
            self.output(404, {"error": "Not found"})
        except (ValueError, OSError) as exc:
            self.output(400, {"error": str(exc)})

    def length(self, maximum):
        size = int(self.headers.get("Content-Length", "0"))
        require(0 < size <= maximum and not self.headers.get("Transfer-Encoding"), "Invalid upload size")
        return size

    def do_POST(self):
        try:
            require(self.headers.get("X-Mirror-Action") == "console", "Missing action header")
            route = urlsplit(self.path).path
            if route == "/api/login":
                payload = json.loads(self.rfile.read(self.length(1024)))
                with self.login_lock:
                    now = time.time()
                    for ip, times in list(self.attempts.items()):
                        remaining = [item for item in times if now - item < 300]
                        if remaining:
                            self.attempts[ip] = remaining
                        else:
                            self.attempts.pop(ip)
                    history = self.attempts.setdefault(self.client_address[0], [])
                    if len(history) >= 12:
                        return self.output(429, {"error": "Too many login attempts. Try again later."})
                    data = credentials()
                    password = payload.get("password")
                    if not isinstance(password, str) or not hmac.compare_digest(
                        data["hash"], password_hash(password, data["salt"])
                    ):
                        history.append(now)
                        return self.output(401, {"error": "Incorrect administrator password"})
                    history.clear()
                return self.output(200, {"admin": True}, headers={"Set-Cookie":
                    f"mirror_session={login_ticket()}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600"})
            if not self.admin():
                return self.output(401, {"error": "Sign in to manage imports"})
            if route == "/api/logout":
                return self.output(200, {"admin": False}, headers={"Set-Cookie":
                    "mirror_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"})
            if route == "/api/jobs":
                size = self.length(MAX_UPLOAD)
                require(self.headers.get("Content-Type") == "application/gzip", "Select a .tar.gz bundle")
                return self.upload(size)
            if route.startswith("/api/jobs/") and route.endswith("/publish"):
                job = self.get_job(route.removeprefix("/api/jobs/").removesuffix("/publish"))
                data = job_data(job)
                require(data["status"] in {"verified", "partial", "publishing"}, "The bundle is not ready for publication")
                require(not PUBLISH.locked(), "Another batch is publishing")
                threading.Thread(target=publish, args=(job,), daemon=True).start()
                return self.output(202, {"status": "queued"})
            if route.startswith("/api/jobs/") and route.endswith("/discard"):
                job = self.get_job(route.removeprefix("/api/jobs/").removesuffix("/discard"))
                with PUBLISH:
                    data = job_data(job)
                    require(data["status"] in {"verified", "partial"}, "Only pending bundles can be discarded")
                    json_atomic(job / "publish.json", {"status": "discarded", "published": data.get("published", {})})
                    shutil.rmtree(job / "bundle", ignore_errors=True)
                    (job / "upload.tar.gz").unlink(missing_ok=True)
                return self.output(200, {"status": "discarded"})
            self.output(404, {"error": "Not found"})
        except (ValueError, OSError) as exc:
            self.output(400, {"error": str(exc)})

    def upload(self, size):
        with self.upload_lock:
            staged = sum((job / "upload.tar.gz").is_file() for job in JOBS.iterdir())
            require(staged < MAX_JOBS, "The pending queue is full")
            require(shutil.disk_usage(STATE).free > size * 5 + 512 * 2**20, "Insufficient staging space")
            job = JOBS / secrets.token_hex(16)
            job.mkdir()
            remaining = size
            try:
                with (job / "upload.tar.gz").open("xb") as output:
                    checksum = hashlib.sha256()
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 2**20))
                        require(chunk, "Upload interrupted")
                        output.write(chunk)
                        checksum.update(chunk)
                        remaining -= len(chunk)
                data = {"id": job.name, "status": "checking", "created": time.time(),
                        "operator": "admin", "bytes": size, "sha256": checksum.hexdigest()}
                json_atomic(job / "upload.json", data)
                return self.output(202, data)
            except Exception:
                shutil.rmtree(job)
                raise


def main():
    mode = sys.argv[1]
    if mode == "init-admin":
        make_credentials()
        return
    JOBS.mkdir(parents=True, exist_ok=True)
    if mode == "worker":
        worker()
        return
    with ThreadingHTTPServer(("0.0.0.0", 18082), Handler) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(SECRETS / "server.crt", SECRETS / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
