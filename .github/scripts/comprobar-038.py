import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile

tmp = Path(os.environ['RUNNER_TEMP'])
root = tmp / 'release-038'
proof = root / 'index.json'
distribucion = 'https://github.com/Vimen1803/werewolf-releases'
api = 'https://api.github.com/repos/Vimen1803/werewolf-releases'
render = 'https://werewolf-ye7x.onrender.com'
paths = {
    'windows': {'client-werewolf.exe': 'client/werewolf.exe', 'WereWolf-Setup.exe': 'launcher/WereWolf-Setup.exe'},
    'linux': {'client-werewolf': 'client/werewolf', 'WereWolf-Linux.deb': 'launcher/WereWolf-Linux.deb'},
    'macos': {'client-werewolf': 'client/werewolf', 'WereWolf-macOS.dmg': 'launcher/WereWolf-macOS.dmg'},
    'android': {'WereWolf-Android.apk': 'launcher/WereWolf-Android.apk'},
}

def require(ok, message):
    if not ok:
        raise ValueError(message)

def fetch(url, accept='application/json'):
    return urlopen(Request(url, headers={'User-Agent': 'WereWolf Release Check', 'Accept': accept}), timeout=45)

def jfetch(url):
    with fetch(url) as response:
        return json.load(response)

def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def preparar():
    source = json.loads((tmp / 'ww-entrada.json').read_text())
    expected = json.loads(Path('transfer-038/iniciar.json').read_text())
    require(source['commit'] == expected['commit'], 'SHA de compilación diferente')
    require(source['run'] == expected['run'], 'Ejecución de compilación diferente')
    require(set(source['archives']) == set(paths), 'Plataformas incompletas')
    items = {}
    for platform, expected_files in paths.items():
        config = source['archives'][platform]
        host = urlsplit(config['url'])
        require(host.scheme == 'https' and host.hostname.endswith('.oaiusercontent.com'), 'Origen de archivo diferente')
        archive = tmp / f'werewolf-038-{platform}.zip'
        try:
            with fetch(config['url'], 'application/octet-stream') as response, archive.open('wb') as file:
                shutil.copyfileobj(response, file)
        except Exception as error:
            raise SystemExit(f'No se pudo recuperar {platform}: {type(error).__name__} {getattr(error, "code", "")}') from None
        require(archive.stat().st_size == config['size'], f'Tamaño del ZIP incorrecto: {platform}')
        if config.get('digest'):
            require(digest_file(archive) == config['digest'].removeprefix('sha256:'), f'Hash del ZIP incorrecto: {platform}')
        with ZipFile(archive) as zipped:
            for name, relative in expected_files.items():
                members = [entry for entry in zipped.infolist() if not entry.is_dir() and Path(entry.filename).name == name]
                require(len(members) == 1, f'Falta o está duplicado {platform}/{name}')
                target_path = f'incoming/{platform}/{relative}'
                target = root / target_path
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(members[0]) as entry, target.open('wb') as output:
                    shutil.copyfileobj(entry, output)
                require(target.stat().st_size > 0, f'Archivo vacío: {target_path}')
                items[target_path] = {'size': target.stat().st_size, 'sha256': digest_file(target)}
                print(f'Comprobado {target_path}')
    (root / 'incoming/VERSION').write_text('0.3.8\n', encoding='utf-8')
    proof.write_text(json.dumps({'version': '0.3.8', 'commit': expected['commit'], 'items': items}), encoding='utf-8')

def verificar():
    expected = json.loads(proof.read_text())['items']
    deadline = time.monotonic() + 680
    while True:
        try:
            latest = jfetch(api + '/releases/latest')
            require(latest.get('tag_name') == 'v0.3.8', 'Esperando etiqueta estable')
            require(jfetch(render + '/health').get('ok') is True, 'Esperando respuesta de Render')
            version = jfetch(render + '/api/version')
            require(version.get('version') == version.get('desktopVersion') == '0.3.8', 'Esperando versión y manifiesto en Render')
            launch = jfetch(api + '/releases/tags/launcher')
            require(launch.get('name') == 'Launcher de WereWolf 0.3.8', 'Esperando los instaladores públicos')
            assets = {entry['name']: entry for entry in launch['assets']}
            platforms = {}
            for platform in ('windows', 'linux', 'macos'):
                remote = jfetch(render + '/api/desktop/manifest?platform=' + platform)
                require(remote.get('version') == '0.3.8' and remote.get('platform') == platform, f'Manifiesto anterior de {platform}')
                name = 'werewolf.exe' if platform == 'windows' else 'werewolf'
                client = expected[f'incoming/{platform}/client/{name}']
                remote_file = next((item for item in remote['files'] if item['path'] == name), {})
                require(all(remote_file.get(key) == value for key, value in client.items()), f'Cliente diferente para {platform}')
                platforms[platform] = remote['version']
            checked = []
            for path, file in expected.items():
                if '/launcher/' not in path:
                    continue
                name = Path(path).name
                require(assets.get(name, {}).get('size') == file['size'], f'Tamaño incorrecto de {name}')
                with fetch(distribucion + '/releases/download/launcher/' + name, 'application/octet-stream') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                require(digest == file['sha256'], f'Hash incorrecto de {name}')
                checked.append(name)
            result = {'version': '0.3.8', 'tag': latest['tag_name'], 'health': True, 'manifests': platforms, 'installers': checked}
            Path('transfer-038/publicado.json').write_text(json.dumps(result), encoding='utf-8')
            print(json.dumps(result))
            return
        except Exception as error:
            if time.monotonic() >= deadline:
                raise
            print(str(error), flush=True)
            time.sleep(12)

if __name__ == '__main__':
    {'preparar': preparar, 'verificar': verificar}[sys.argv[1]]()
