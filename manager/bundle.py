"""Build and verify dependency-only bundles. Never execute package code."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request
from urllib.parse import urlsplit
from zipfile import ZipFile

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from pip._vendor.packaging.version import Version

PROFILES = {"cp310": "/usr/local/bin/python3.10", "cp312": "/usr/local/bin/python3.12"}
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_BYTES", 1024**3))
MAX_EXPANDED = 4 * MAX_UPLOAD
MAX_FILES = 12000
NPM_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z")
NPM_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,239}\Z")
NPM_SPEC = re.compile(r"[0-9A-Za-z*~^<>=| .+_-]+\Z")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def dump(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read(path):
    return json.loads(path.read_bytes())


def npm_key(name, version):
    require(isinstance(name, str) and NPM_NAME.fullmatch(name), f"Invalid npm name: {name!r}")
    require(isinstance(version, str) and NPM_VERSION.fullmatch(version), f"Invalid npm version: {version!r}")
    return name, version


def npm_filename(name, version):
    return f"{name.split('/')[-1]}-{version}.tgz"


def npm_dependencies(manifest, identity, *, include_dev=False):
    fields = ("dependencies", "optionalDependencies", "peerDependencies")
    if include_dev:
        fields += ("devDependencies",)
    for field in fields:
        for dep, spec in manifest.get(field, {}).items():
            alias = spec[4:].rpartition("@") if isinstance(spec, str) and spec.startswith("npm:") else None
            valid_alias = alias and NPM_NAME.fullmatch(alias[0]) and NPM_SPEC.fullmatch(alias[2])
            require(isinstance(spec, str) and (NPM_SPEC.fullmatch(spec) or valid_alias),
                    f"{identity} -> {dep}: unsupported dependency {spec!r}")


def npm_manifest(path):
    with tarfile.open(path, "r:gz") as tar:
        target = None
        total = 0
        for i, member in enumerate(tar):
            total += member.size
            parts = PurePosixPath(member.name).parts
            require(i < MAX_FILES and total <= MAX_EXPANDED, f"Tarball exceeds limits: {path.name}")
            require(parts and parts[0] == "package" and ".." not in parts and
                    (member.isfile() or member.isdir()), f"Unsafe member in {path.name}: {member.name}")
            if member.name == "package/package.json":
                require(target is None and member.isfile() and member.size <= 2**20, "Invalid package.json")
                target = json.load(tar.extractfile(member))
    require(isinstance(target, dict), f"Missing package/package.json: {path.name}")
    name, version = npm_key(target.get("name"), target.get("version"))
    scripts = target.get("scripts", {})
    lifecycle = sorted(set(scripts) & {"preinstall", "install", "postinstall"})
    require(not lifecycle and not target.get("gypfile"),
            f"{name}@{version} requires installation review: {', '.join(lifecycle) or 'node-gyp'}")
    npm_dependencies(target, f"{name}@{version}")
    return target


def check_integrity(path, value):
    require(isinstance(value, str), f"Missing npm integrity for {path.name}")
    candidates = value.split()
    for algorithm in ("sha512", "sha384", "sha256", "sha1"):
        supported = [entry.split("?", 1)[0] for entry in candidates if entry.startswith(algorithm + "-")]
        if supported:
            actual = base64.b64encode(bytes.fromhex(digest(path, algorithm))).decode()
            require(algorithm + "-" + actual in supported, f"npm integrity mismatch: {path.name}")
            return
    raise ValueError(f"Unsupported npm integrity for {path.name}")


def compatible(entry):
    for field, value in (("os", "linux"), ("cpu", "x64"), ("libc", "glibc")):
        values = entry.get(field, [])
        if not values:
            continue
        if isinstance(values, str):
            values = [values]
        if "!" + value in values or ("any" not in values and value not in values
                                      and any(not item.startswith("!") for item in values)):
            return False
    return True


def locked_npm(lock):
    require(lock.get("lockfileVersion") == 3 and isinstance(lock.get("packages"), dict),
            "A version 3 npm lockfile is required")
    for path, entry in lock["packages"].items():
        if not path or entry.get("dev") or entry.get("inBundle"):
            continue
        require(not entry.get("link") and path.startswith("node_modules/"), f"Unsupported npm entry: {path}")
        if not compatible(entry):
            require(entry.get("optional"), f"Incompatible mandatory dependency: {path}")
            continue
        name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
        name, version = npm_key(name, entry.get("version"))
        require(entry.get("resolved") and entry.get("integrity"), f"Missing resolved/integrity: {path}")
        yield path, name, version, entry


def run(argv, cwd, *, online=False):
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), "LANG": "C.UTF-8",
           "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "NPM_CONFIG_USERCONFIG": str(cwd / ".bundle-user.npmrc"),
           "NPM_CONFIG_GLOBALCONFIG": str(cwd / ".bundle-global.npmrc"),
           "NPM_CONFIG_FETCH_RETRIES": "0"}
    if online:
        env.update({k: v for k, v in os.environ.items() if k.lower() in
                    {"http_proxy", "https_proxy", "no_proxy", "ssl_cert_file"}})
    process = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if process.returncode:
        raise ValueError(process.stdout[-1500:] + "\n" + process.stderr[-6500:])
    return process.stdout


def target():
    require(platform.system() == "Linux" and platform.machine() in {"x86_64", "AMD64"},
            "Run the tool in its Linux AMD64 image")


def requirements(text, *, locked=False):
    lines = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec = Requirement(line)
        require(not spec.url and not line.startswith("-"), f"Unsupported Python requirement: {line}")
        if locked:
            pins = list(spec.specifier)
            require(len(pins) == 1 and pins[0].operator == "==" and "*" not in pins[0].version
                    and not spec.marker and not spec.extras, f"Expected an exact pin: {line}")
        lines.append(line)
    require(lines, "The dependency list is empty")
    return lines


def wheel_info(path):
    name, version, _, tags = parse_wheel_filename(path.name)
    with ZipFile(path) as z:
        files = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        require(len(files) == 1 and z.getinfo(files[0]).file_size <= 2**20,
                f"Invalid wheel metadata: {path.name}")
        from email.parser import BytesParser
        metadata = BytesParser().parsebytes(z.read(files[0]))
    require(canonicalize_name(metadata.get("Name", "")) == name and metadata.get("Version") and
            version == Version(metadata["Version"]),
            f"Wheel identity does not match metadata: {path.name}")
    return str(name), str(version), sorted(map(str, tags))


def inspect_bundle(archive, directory):
    require(archive.stat().st_size <= MAX_UPLOAD, "Archive exceeds the upload limit")
    directory.mkdir(parents=True)
    paths, total = set(), 0
    with tarfile.open(archive, "r|gz") as tar:
        for member in tar:
            key = member.name
            parts = key.split("/")
            allowed = ((len(parts) == 1 and key in
                        {"manifest.json", "requirements.in", "requirements.lock", "package.json", "package-lock.json"})
                       or len(parts) == 2 and parts[0] == "files" and FILE_NAME.fullmatch(parts[1]))
            total += member.size
            require(member.isfile() and allowed and key not in paths, f"Unsafe or duplicate entry: {key}")
            require(len(paths) < MAX_FILES and total <= MAX_EXPANDED and
                    (len(parts) > 1 or member.size <= 32 * 2**20), "Archive exceeds extraction limits")
            paths.add(key)
            output = directory / key
            output.parent.mkdir(exist_ok=True)
            with output.open("xb") as stream:
                shutil.copyfileobj(tar.extractfile(member), stream, length=1024 * 1024)
    manifest = read(directory / "manifest.json")
    kind, profile = manifest.get("kind"), manifest.get("profile")
    require(manifest.get("schema") == 1 and
            (kind == "pypi" and profile in PROFILES or kind == "npm" and profile == "node24"),
            "Unsupported bundle format or runtime profile")
    controls = {"requirements.in", "requirements.lock"} if kind == "pypi" else {"package.json", "package-lock.json"}
    hashes = manifest["sha256"]
    require(isinstance(hashes, dict) and set(hashes) == paths - {"manifest.json"} and controls <= set(hashes),
            "Manifest and archive file lists differ")
    for rel, checksum in hashes.items():
        require(digest(directory / rel) == checksum, f"SHA256 mismatch: {rel}")
    files = {}
    for rel in sorted(paths - controls - {"manifest.json"}):
        require(rel.startswith("files/"), f"Unexpected control file: {rel}")
        path = directory / rel
        if kind == "pypi":
            name, version, tags = wheel_info(path)
            files[rel] = {"name": name, "version": version, "tags": tags}
        else:
            meta = npm_manifest(path)
            name, version = npm_key(meta["name"], meta["version"])
            files[rel] = {"name": name, "version": version}
    require(files, "Bundle contains no dependency files")
    return manifest, files


def verify(directory, manifest, files):
    target()
    if manifest["kind"] == "pypi":
        original = (directory / "requirements.in").read_text()
        pinned = (directory / "requirements.lock").read_text()
        requirements(original)
        pins = requirements(pinned, locked=True)
        available = {(row["name"], row["version"]) for row in files.values()}
        expected = {(canonicalize_name(spec.name), next(iter(spec.specifier)).version)
                    for spec in map(Requirement, pins)}
        require(available == expected, f"Wheel/lock mismatch: missing {sorted(expected - available)}, extra {sorted(available - expected)}")
        run([PROFILES[manifest["profile"]], "-m", "pip", "install", "--dry-run", "--ignore-installed",
             "--no-cache-dir", "--no-index", "--only-binary=:all:", "--find-links", str(directory / "files"),
             "-c", "requirements.lock", "-r", "requirements.in", "-r", "requirements.lock"], directory)
        return
    package_lock = read(directory / "package-lock.json")
    npm_dependencies(read(directory / "package.json"), "package.json", include_dev=True)
    by_id = {(row["name"], row["version"]): directory / rel for rel, row in files.items()}
    require(len(by_id) == len(files), "Duplicate npm name and version")
    wanted = list(locked_npm(package_lock))
    expected = {(name, version) for _, name, version, _ in wanted}
    require(set(by_id) == expected, f"npm files/lock mismatch: missing {sorted(expected - set(by_id))}, extra {sorted(set(by_id) - expected)}")
    require(run(["node", "-p", "process.versions.node.split('.')[0]"], directory).strip() == "24",
            "Node 24 is required")
    urls = {}

    class Artifacts(BaseHTTPRequestHandler):
        def do_GET(self):
            asset = urls.get(self.path)
            if asset is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(asset.stat().st_size))
            self.end_headers()
            with asset.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Artifacts) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        for _, name, version, entry in wanted:
            asset = by_id[name, version]
            check_integrity(asset, entry["integrity"])
            route = "/" + asset.name
            urls[route] = asset
            entry["resolved"] = base + route
        for path, entry in package_lock["packages"].items():
            if path and "resolved" in entry and not entry["resolved"].startswith(base + "/"):
                entry["resolved"] = base + "/omitted.tgz"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=directory) as install:
                install = Path(install)
                shutil.copyfile(directory / "package.json", install / "package.json")
                dump(install / "package-lock.json", package_lock)
                run(["npm", "ci", "--ignore-scripts", "--omit=dev", "--include=optional", "--include=peer",
                     "--engine-strict", "--strict-peer-deps", "--no-audit", "--no-fund", "--registry=" + base], install)
        finally:
            server.shutdown()
            thread.join()


def download(url, output, hosts, max_size=MAX_UPLOAD):
    def allowed(url):
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in hosts and not parsed.username and not parsed.password

    class RestrictedRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            require(allowed(newurl), f"Unapproved download redirect: {newurl}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    require(allowed(url), f"Unapproved download URL: {url}")
    with request.build_opener(RestrictedRedirect()).open(url, timeout=90) as source, output.open("xb") as stream:
        total = 0
        for chunk in iter(lambda: source.read(2**20), b""):
            total += len(chunk)
            require(total <= max_size, f"File exceeds limit: {url}")
            stream.write(chunk)


def build(args):
    target()
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging)
        artifacts = root / "files"
        artifacts.mkdir()
        if args.profile in PROFILES:
            kind = "pypi"
            pinned = requirements(args.input.read_text())
            (root / "requirements.in").write_text("\n".join(pinned) + "\n")
            run([PROFILES[args.profile], "-m", "pip", "download", "--only-binary=:all:",
                 "--index-url", "https://pypi.org/simple", "-r", "requirements.in", "-d", "files"], root, online=True)
            packages = sorted({wheel_info(path)[:2] for path in artifacts.glob("*.whl")})
            (root / "requirements.lock").write_text("".join(f"{name}=={version}\n" for name, version in packages))
            source = "https://pypi.org/simple"
        else:
            kind = "npm"
            source = "https://registry.npmjs.org"
            npm_dependencies(read(args.input), "package.json", include_dev=True)
            shutil.copyfile(args.input, root / "package.json")
            lock = args.input.with_name("package-lock.json")
            if lock.is_file():
                shutil.copyfile(lock, root / "package-lock.json")
            else:
                run(["npm", "install", "--package-lock-only", "--ignore-scripts", "--omit=dev",
                     "--no-audit", "--no-fund", "--registry=" + source], root, online=True)
            hosts = {"registry.npmjs.org", "registry.npmmirror.com"}
            for _, name, version, entry in locked_npm(read(root / "package-lock.json")):
                identity = hashlib.sha256(f"{name}@{version}".encode()).hexdigest()[:16]
                path = artifacts / f"{identity}-{npm_filename(name, version)}"
                if not path.exists():
                    download(entry["resolved"], path, hosts, max_size=48 * 2**20)
                check_integrity(path, entry["integrity"])
        paths = sorted(path for path in root.rglob("*") if path.is_file() and
                       (path.parent == artifacts or path.name in
                        {"requirements.in", "requirements.lock", "package.json", "package-lock.json"}))
        manifest = {"schema": 1, "kind": kind, "profile": args.profile, "source": source,
                    "sha256": {str(path.relative_to(root)): digest(path) for path in paths}}
        dump(root / "manifest.json", manifest)
        temporary = args.output.with_name(args.output.name + ".pending")
        with tarfile.open(temporary, "x:gz") as tar:
            for path in [root / "manifest.json", *paths]:
                tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
        try:
            check(temporary)
            require(not args.output.exists(), f"Output already exists: {args.output}")
            temporary.rename(args.output)
        finally:
            temporary.unlink(missing_ok=True)
    print(args.output)


def check(archive):
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging) / "bundle"
        manifest, files = inspect_bundle(archive, root)
        verify(root, manifest, files)
        return {"profile": manifest["profile"], "kind": manifest["kind"], "files": len(files),
                "sha256": digest(archive), "result": "passed"}


def main():
    parser = argparse.ArgumentParser(prog="mirror-bundle")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--profile", required=True, choices=[*PROFILES, "node24"])
    build_parser.add_argument("--input", required=True, type=lambda value: Path(value).resolve())
    build_parser.add_argument("--output", required=True, type=lambda value: Path(value).resolve())
    check_parser = commands.add_parser("check")
    check_parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args)
    else:
        print(json.dumps(check(args.archive)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        raise SystemExit(str(exc))
