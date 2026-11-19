#!/usr/bin/env python3
"""Publica WereWolf en GitHub Releases (gratis) para que el launcher lo descargue.

Uso habitual:

    python distribution/publish_build.py --client
        → publica desktop/target/release/werewolf(.exe) del sistema en el que estás
    python distribution/publish_build.py --build windows=dir1 --build linux=dir2 --build macos=dir3
        → publica una versión con las builds de varios sistemas a la vez
    python distribution/publish_build.py ruta/a/la/build
        → publica una carpeta de Windows (werewolf.exe + otros archivos)
    python distribution/publish_build.py --launcher [sistema=]ruta/al/instalador
        → actualiza el instalador del launcher de ese sistema (por defecto, windows)

Cada sistema tiene su manifiesto en la misma release: version.json (Windows),
version-linux.json y version-macos.json. La web sirve el que pide el launcher.

Configuración (variables de entorno o distribution/.env):
    GITHUB_RELEASES_REPO  repo PÚBLICO de descargas, p. ej. Vimen1803/werewolf-releases
    GITHUB_TOKEN          token con permiso «Contents: Read and write» en ese repo
                          (si falta, se usa el de `gh auth token`)

Cómo funciona:
  1. Calcula el SHA-256 y el tamaño de cada archivo de la build.
  2. Crea la release v<versión> y sube solo los archivos cuyo contenido no estaba
     ya publicado; para los que no cambian, el manifiesto apunta a la release en
     la que ya estaban (nada se sube ni se descarga dos veces).
  3. Sube version.json y marca la release como la última. Desde ese momento es
     la única versión que puede jugar: la web lee
     https://github.com/<repo>/releases/latest/download/version.json
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = "werewolf.exe"
LAUNCHER_TAG = "launcher"
LAUNCHER_ASSET = "WereWolf-Setup.exe"
MANIFEST_ASSET = "version.json"
PLATFORMS = ("windows", "linux", "macos")
EXE_BY_PLATFORM = {"windows": "werewolf.exe", "linux": "werewolf", "macos": "werewolf"}
# Android no tiene launcher ni manifiesto: la APK es la app entera.
LAUNCHER_ASSETS = {"windows": "WereWolf-Setup.exe", "linux": "WereWolf-Linux.deb", "macos": "WereWolf-macOS.dmg", "android": "WereWolf-Android.apk"}


def manifest_asset(platform: str) -> str:
    return MANIFEST_ASSET if platform == "windows" else f"version-{platform}.json"


def host_platform() -> str:
    return "windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin" else "linux"
DEFAULT_EXCLUDES = ["*.pdb", "*.ilk", "*.log", ".DS_Store", "Thumbs.db", "desktop.ini"]


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cargo_version() -> str | None:
    """Versión del juego según desktop/Cargo.toml (la sube `npm run release:*`)."""
    try:
        text = (ROOT / "desktop" / "Cargo.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.split("[workspace.package]", 1)[-1].splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_name(relative: str, platform: str = "windows") -> str:
    """Las releases son planas: data/a.pak se sube como data__a.pak (y con el
    sistema delante fuera de Windows, porque Linux y macOS comparten nombres)."""
    flat = relative.replace("/", "__")
    return flat if platform == "windows" else f"{platform}__{flat}"


def scan_build(root: Path, excludes: list[str]) -> list[dict]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern) for pattern in excludes):
            continue
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_of(path)})
    return files


class GitHub:
    """Cliente mínimo de la API de GitHub Releases (solo biblioteca estándar)."""

    def __init__(self, repo: str, token: str, api: str | None = None, web: str = "https://github.com"):
        self.repo, self.token = repo, token
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.web = web.rstrip("/")

    def download_url(self, tag: str, name: str) -> str:
        return f"{self.web}/{self.repo}/releases/download/{tag}/{urllib.parse.quote(name)}"

    def request(self, method: str, url: str, body=None, data: bytes | None = None, content_type: str = "application/json"):
        if not url.startswith("http"):
            url = f"{self.api}/repos/{self.repo}{url}"
        payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(url, data=payload, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": content_type,
            "User-Agent": "werewolf-publish",
        })
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise SystemExit(f"GitHub respondió {error.code} en {method} {url}: {error.read().decode(errors='replace')[:300]}")

    def release_by_tag(self, tag: str):
        return self.request("GET", f"/releases/tags/{urllib.parse.quote(tag)}")

    def latest(self):
        return self.request("GET", "/releases/latest")

    def create_release(self, tag: str, name: str, body: str):
        # Nunca nace como «latest»: hasta que version.json está subido, los
        # jugadores siguen viendo la versión anterior completa.
        return self.request("POST", "/releases", {"tag_name": tag, "name": name, "body": body, "draft": False, "prerelease": False, "make_latest": "false"})

    def update_release(self, release_id: int, **fields):
        return self.request("PATCH", f"/releases/{release_id}", fields)

    def upload(self, release: dict, path: Path, name: str) -> dict:
        for asset in release.get("assets", []):
            if asset["name"] == name:
                self.request("DELETE", f"/releases/assets/{asset['id']}")
        url = release["upload_url"].split("{")[0] + "?" + urllib.parse.urlencode({"name": name})
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self.request("POST", url, data=path.read_bytes(), content_type=content_type)

    def asset_json(self, release: dict, name: str):
        """Lee un asset JSON de una release (version.json de la versión anterior)."""
        for asset in release.get("assets", []):
            if asset["name"] == name:
                request = urllib.request.Request(asset["url"], headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/octet-stream",
                    "User-Agent": "werewolf-publish",
                })
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read())
        return None


def github_from_env() -> GitHub:
    repo = os.environ.get("GITHUB_RELEASES_REPO", "").strip()
    if not repo or "/" not in repo:
        raise SystemExit("Falta GITHUB_RELEASES_REPO (p. ej. Vimen1803/werewolf-releases) en distribution/.env")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            token = ""
    if not token:
        raise SystemExit("Falta GITHUB_TOKEN en distribution/.env (o inicia sesión con `gh auth login`).")
    return GitHub(repo, token)


def publish(build_dir: Path, version: str, exe: str, excludes: list[str], dry_run: bool = False, gh: GitHub | None = None) -> dict:
    """Publica solo Windows (compatibilidad con el uso anterior)."""
    return publish_many({"windows": (build_dir, exe)}, version, excludes, dry_run, gh)


def publish_many(builds: dict, version: str, excludes: list[str], dry_run: bool = False, gh: GitHub | None = None) -> dict:
    """builds: {sistema: (carpeta, ejecutable)}. Todas van en la release v<versión>."""
    scanned = {}
    for platform, (build_dir, exe) in builds.items():
        if platform not in PLATFORMS:
            raise SystemExit(f"Sistema desconocido: {platform} (usa {', '.join(PLATFORMS)})")
        if not build_dir.is_dir():
            raise SystemExit(f"No existe la carpeta de la build de {platform}: {build_dir}")
        if not (build_dir / exe).is_file():
            raise SystemExit(f"El ejecutable '{exe}' no está dentro de {build_dir}")
        print(f"[{platform}] calculando hashes en {build_dir} …")
        scanned[platform] = scan_build(build_dir, excludes)
    missing = [p for p in PLATFORMS if p not in builds]
    if missing:
        print(f"Aviso: esta versión no incluye {', '.join(missing)}; esos jugadores no podrán actualizar hasta que publiques su build.")

    if gh is None and not dry_run:
        gh = github_from_env()
    tag = f"v{version}"
    known: dict[str, str] = {}
    if gh:
        if gh.release_by_tag(tag):
            raise SystemExit(f"La versión {version} ya está publicada. Súbela con `npm run release:patch` y vuelve a compilar.")
        latest = gh.latest()
        if latest:
            for platform in PLATFORMS:
                previous = gh.asset_json(latest, manifest_asset(platform))
                for item in (previous or {}).get("files", []):
                    known[item["sha256"]] = item["url"]
            print(f"Versión publicada ahora: {latest['tag_name']}")

    target = gh or GitHub("OWNER/REPO", "")
    manifests, pending = {}, []
    for platform, files in scanned.items():
        entries = []
        for item in files:
            if item["sha256"] not in known:
                name = asset_name(item["path"], platform)
                known[item["sha256"]] = target.download_url(tag, name)  # copias idénticas: una sola subida
                pending.append((platform, item, name))
            entries.append({**item, "url": known[item["sha256"]]})
        manifests[platform] = {
            "game": "werewolf",
            "channel": "stable",
            "platform": platform,
            "version": version,
            "exe": builds[platform][1],
            "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "totalSize": sum(item["size"] for item in files),
            "files": entries,
        }
    print(f"{len(pending)} archivos nuevos o modificados.")
    if dry_run:
        print(json.dumps(manifests, indent=2, ensure_ascii=False)[:3000])
        return {"manifest": manifests.get("windows"), "manifests": manifests, "uploaded": []}

    release = gh.create_release(tag, f"WereWolf {version}", f"Cliente de escritorio {version} ({', '.join(builds)}).")
    uploaded = []
    for index, (platform, item, name) in enumerate(pending, 1):
        gh.upload(release, builds[platform][0] / item["path"], name)
        uploaded.append(item["path"] if platform == "windows" else f"{platform}:{item['path']}")
        print(f"  [{index}/{len(pending)}] {platform}: {item['path']}")
    with tempfile.TemporaryDirectory() as folder:
        for platform, manifest in manifests.items():
            path = Path(folder) / manifest_asset(platform)
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            gh.upload(release, path, path.name)
    gh.update_release(release["id"], make_latest="true")
    print(f"Publicada la versión {version}. Manifiesto: {gh.web}/{gh.repo}/releases/latest/download/{MANIFEST_ASSET}")
    return {"manifest": manifests.get("windows"), "manifests": manifests, "uploaded": uploaded}


def publish_launcher(installer: Path, version: str, gh: GitHub | None = None, platform: str = "windows") -> str:
    if not installer.is_file():
        raise SystemExit(f"No existe el instalador: {installer}")
    if platform not in LAUNCHER_ASSETS:
        raise SystemExit(f"Sistema desconocido: {platform}")
    gh = gh or github_from_env()
    asset = LAUNCHER_ASSETS[platform]
    release = gh.release_by_tag(LAUNCHER_TAG) or gh.create_release(LAUNCHER_TAG, "Launcher de WereWolf", "Instaladores del launcher (Windows, Linux y macOS) y la app de Android.")
    gh.upload(release, installer, asset)
    gh.update_release(release["id"], name=f"Launcher de WereWolf {version}", make_latest="false")
    url = gh.download_url(LAUNCHER_TAG, asset)
    print(f"Launcher {version} ({platform}) publicado: {url}")
    return url


def split_platform(value: str, default: str = "windows") -> tuple[str, Path]:
    head, sep, tail = value.partition("=")
    if sep and (head in PLATFORMS or head in LAUNCHER_ASSETS):
        return head, Path(tail)
    return default, Path(value)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description="Publica WereWolf en GitHub Releases.")
    parser.add_argument("build_dir", type=Path, nargs="?", help="Carpeta con la build de Windows (si no usas --client ni --build)")
    parser.add_argument("--client", action="store_true", help="Publica desktop/target/release/werewolf(.exe) de este sistema")
    parser.add_argument("--build", action="append", default=[], metavar="SISTEMA=CARPETA", help="Build de un sistema (windows, linux, macos); se puede repetir")
    parser.add_argument("--launcher", action="append", default=[], metavar="[SISTEMA=]INSTALADOR", help="Sube el instalador del launcher de ese sistema (por defecto, windows); se puede repetir")
    parser.add_argument("--version", help="Versión (por defecto, la de desktop/Cargo.toml)")
    parser.add_argument("--exe", default=DEFAULT_EXE, help="Ejecutable del juego, relativo a la carpeta de la build")
    parser.add_argument("--exclude", action="append", default=[], help="Patrón a excluir (se puede repetir)")
    parser.add_argument("--dry-run", action="store_true", help="Solo calcula el manifiesto, sin subir nada")
    args = parser.parse_args(argv)

    expected = cargo_version()
    version = args.version or expected
    if not version:
        parser.error("No se pudo leer la versión de desktop/Cargo.toml: indícala con --version.")
    if expected and args.version and args.version != expected:
        print(f"Aviso: --version {args.version} no coincide con desktop/Cargo.toml ({expected}). El ejecutable informa de la versión con la que se compiló.")

    excludes = DEFAULT_EXCLUDES + args.exclude
    builds = {}
    for value in args.build:
        platform, folder = split_platform(value, "")
        if not platform:
            parser.error(f"--build necesita SISTEMA=CARPETA: {value}")
        builds[platform] = (folder, EXE_BY_PLATFORM[platform])
    if args.build_dir:
        builds["windows"] = (args.build_dir, args.exe.replace("\\", "/"))
    with tempfile.TemporaryDirectory() as folder:
        if args.client:
            platform = host_platform()
            exe = EXE_BY_PLATFORM[platform]
            built = ROOT / "desktop" / "target" / "release" / exe
            if not built.is_file():
                raise SystemExit(f"No existe {built}. Compila antes: cd desktop && cargo build --release -p werewolf")
            shutil.copy2(built, Path(folder) / exe)
            builds[platform] = (Path(folder), exe)
        if builds:
            publish_many(builds, version, excludes, args.dry_run)
        for value in args.launcher:
            platform, installer = split_platform(value)
            publish_launcher(installer, version, platform=platform)
    if not builds and not args.launcher:
        parser.error("Indica la carpeta de la build, --build, --client o --launcher.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
