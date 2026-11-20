"""Registry inventory, one-time Verdaccio migration, and live publication."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from bundle import digest, npm_filename, npm_key, npm_manifest, read, require, wheel_info

ROOT = Path(os.environ.get("MIRROR_DATA", "/data"))
WHEELS = ROOT / "pypi/packages"
NPM = Path(os.environ.get("NPM_STORAGE", ROOT / "npm/storage"))
NPM_URL = os.environ.get("NPM_INTERNAL_URL", "http://local-npm:4873")
PYPI_URL = os.environ.get("PYPI_INTERNAL_URL", "http://local-pypi:8081")
HTTP = build_opener(ProxyHandler({}))


def json_atomic(path, data):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temp:
        json.dump(data, temp, ensure_ascii=False)
        temp.write("\n")
        temp.flush()
        os.fsync(temp.fileno())
    os.replace(temp.name, path)


def npm_records(root=NPM):
    for file in sorted(root.glob("*/package.json")) + sorted(root.glob("@*/*/package.json")):
        pkg = read(file)
        if not isinstance(pkg, dict) or not isinstance(pkg.get("versions"), dict):
            continue
        rows = {}
        for version, info in pkg["versions"].items():
            try:
                name, version = npm_key(pkg.get("name"), version)
                tarball = npm_filename(name, version)
                dist = info["dist"]
                recorded = Path(urlsplit(dist["tarball"]).path).name
                if recorded == tarball and (file.parent / tarball).is_file():
                    rows[version] = (file.parent / tarball, info)
            except (ValueError, TypeError, KeyError):
                continue
        yield file, pkg, rows


def npm_index():
    return {(pkg["name"], version): row[0]
            for _, pkg, rows in npm_records() for version, row in rows.items()}


def wheel_index():
    result = {}
    for path in sorted(WHEELS.rglob("*.whl")):
        if path.is_file() and all(not parent.startswith(".") for parent in path.relative_to(WHEELS).parts):
            result.setdefault(path.name, path)
    return result


def catalog():
    python = []
    for name, path in wheel_index().items():
        from pip._vendor.packaging.utils import parse_wheel_filename
        try:
            project, version, _, tags = parse_wheel_filename(name)
            python.append({"name": str(project), "version": str(version), "file": name,
                           "tags": sorted(map(str, tags))})
        except ValueError:
            continue
    npm = [{"name": name, "version": version, "file": path.name}
           for (name, version), path in sorted(npm_index().items())]
    return {"pypi": python, "npm": npm}


def prepare(root):
    """Edit an offline copy only. The original storage is the rollback backup."""
    total, removed = 0, 0
    for file, pkg, rows in npm_records(root):
        total += len(rows)
        removed += len(pkg["versions"]) - len(rows)
        for version, (absolute, meta) in rows.items():
            sha = meta["dist"].get("shasum")
            require(not sha or digest(absolute, "sha1") == sha, f"Bad cached npm tarball: {absolute}")
        pkg["versions"] = {version: info for version, (_, info) in rows.items()}
        tags = {tag: version for tag, version in pkg.get("dist-tags", {}).items() if version in rows}
        pkg["dist-tags"] = tags
        names = {asset.name for asset, _ in rows.values()}
        pkg["_attachments"] = {name: attachment for name, attachment in pkg.get("_attachments", {}).items()
                               if name in names}
        pkg["_distfiles"] = {}
        pkg["_uplinks"] = {}
        pkg["time"] = {key: value for key, value in pkg.get("time", {}).items()
                       if key in rows or key in {"created", "modified"}}
        owner = file.stat()
        json_atomic(file, pkg)
        if os.geteuid() == 0:
            os.chown(file, owner.st_uid, owner.st_gid)
    return {"available": total, "removed_metadata": removed}


def fetch(url, method="GET", data=None, auth=None):
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    if data is not None:
        headers["Content-Type"] = "application/json"
    return HTTP.open(Request(url, data, headers, method=method), timeout=90)


def endpoint(url, suffix):
    try:
        with fetch(url + suffix) as response:
            return response.status == 200
    except OSError:
        return False


def npm_metadata(name):
    try:
        with fetch(f"{NPM_URL}/{quote(name, safe='')}") as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def download_matches(url, expected, algorithm="sha256"):
    try:
        with HTTP.open(url, timeout=60) as response:
            return hashlib.file_digest(response, algorithm).hexdigest() == expected
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def publish_npm(path, name, version, checksum, auth):
    file = npm_filename(name, version)
    key = quote(name, safe="")
    url = f"{NPM_URL}/{key}"
    # The download endpoint uses an unescaped scope separator.
    tar_url = f"{NPM_URL}/{quote(name, safe='/@')}/-/{quote(file)}"
    old = npm_metadata(name)
    if version in old.get("versions", {}):
        require(download_matches(tar_url, checksum), f"{name}@{version} already exists with different or missing bytes")
        return "existing"
    require(path.stat().st_size <= 48 * 2**20, f"npm tarball exceeds 48 MiB: {file}")
    release = npm_manifest(path)
    sha1 = digest(path, "sha1")
    sri = "sha512-" + base64.b64encode(bytes.fromhex(digest(path, "sha512"))).decode()
    release.update({"_id": f"{name}@{version}", "dist": {"shasum": sha1, "integrity": sri, "tarball": tar_url}})
    payload = {"_id": name, "name": name, "description": release.get("description", ""),
               "dist-tags": {"offline-import": version}, "versions": {version: release},
               "_attachments": {file: {"content_type": "application/octet-stream",
                                      "length": path.stat().st_size, "data": base64.b64encode(path.read_bytes()).decode()}}}
    try:
        with fetch(url, "PUT", json.dumps(payload).encode(), auth):
            pass
    except HTTPError as exc:
        if version not in npm_metadata(name).get("versions", {}) or not download_matches(tar_url, checksum):
            message = exc.read(500).decode(errors="replace")
            raise ValueError(f"{name}@{version}: Verdaccio HTTP {exc.code} {message}") from exc
    require(version in npm_metadata(name).get("versions", {}) and download_matches(tar_url, checksum),
            f"{name}@{version} did not pass registry download verification")
    previous = old.get("dist-tags", {}).get("latest")
    desired = previous or (version if "-" not in version.split("+", 1)[0] else None)
    if desired and npm_metadata(name).get("dist-tags", {}).get("latest") != desired:
        with fetch(f"{NPM_URL}/-/package/{key}/dist-tags/latest", "PUT", json.dumps(desired).encode(), auth):
            pass
    return "published"


def publish_wheels(job, items, source):
    destination = WHEELS / f"batch-{job}"
    if not destination.exists() and items:
        with tempfile.TemporaryDirectory(dir=WHEELS.parent) as temporary:
            batch = Path(temporary) / "batch"
            batch.mkdir()
            for rel in items:
                path = batch / Path(rel).name
                shutil.copyfile(source / rel, path)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            batch.rename(destination)
    for path in (destination.iterdir() if destination.exists() else []):
        name, _, _ = wheel_info(path)
        with HTTP.open(f"{PYPI_URL}/simple/{quote(name)}/", timeout=20) as index:
            require(path.name.encode() in index.read(), f"Wheel not visible at PyPI: {path.name}")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare-npm"])
    parser.add_argument("storage", type=Path)
    args = parser.parse_args()
    require(args.storage.is_dir(), "Missing npm storage copy")
    print(json.dumps(prepare(args.storage)))
