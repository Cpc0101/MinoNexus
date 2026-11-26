"""Bind a homepage artifact and its deployment sources to a verified Workbench commit."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

FILES = ('deploy/scripts/install-homepage-nginx.py', 'deploy/scripts/deploy-homepage.sh',
         'deploy/nginx/mino-homepage.conf', 'deploy/nginx/mino-default-https.conf', 'site/site.config.json')


def verify(build, commit, config):
    payload = json.loads((build / 'BUILD.json').read_text())
    number = config.get('icpNumber', '')
    if (payload.get('site') != 'minocc.top' or payload.get('preview') is not False
            or payload.get('source_commit') != commit or payload.get('icpNumber') != number
            or not re.fullmatch(r'[\u4e00-\u9fff]ICP备\d+号(?:-\d+)?', number)):
        raise ValueError('Homepage provenance or ICP configuration mismatch')
    files = {'index.html', 'styles.css', 'main.js', 'favicon.svg'}
    if set(payload.get('sha256', {})) != files or {p.relative_to(build).as_posix() for p in build.rglob('*')} != files | {'BUILD.json'}:
        raise ValueError('Incomplete or extra homepage assets')
    for name in files:
        p = build / name
        if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != payload['sha256'][name]:
            raise ValueError('Homepage asset hash mismatch')
    footer = re.search(r'<footer\b[\s\S]*?</footer>', (build / 'index.html').read_text())
    if not footer or f'id="icp-link">{number}</a>' not in footer[0] or 'href="https://beian.miit.gov.cn/"' not in footer[0]:
        raise ValueError('Homepage ICP footer mismatch')
    return payload


def prepare(package, commit, output):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid source commit')
    output = Path(output)
    output.mkdir()
    build = output / 'build'
    build.mkdir()
    spec = importlib.util.spec_from_file_location('release_validator', Path(__file__).with_name('validate-workbench-release.py'))
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.extract(package, build)
    hashes = {}
    for name in FILES:
        result = subprocess.run(['gh', 'api', f'repos/Cpc0101/MinoWorkbench/contents/{name}?ref={commit}'],
                                check=True, capture_output=True, text=True)
        blob = json.loads(result.stdout)
        content = base64.b64decode(blob['content'])
        if blob.get('type') != 'file' or hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest() != blob['sha']:
            raise ValueError('Invalid pinned source blob')
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    payload = verify(build, commit, json.loads((output / 'site/site.config.json').read_text()))
    (output / 'SOURCE.json').write_text(json.dumps({'source_commit': commit, 'sha256': hashes}, indent=2) + '\n')
    print('Prepared pinned homepage source and complete asset manifest:', commit, payload['icpNumber'])


if __name__ == '__main__':
    try:
        prepare(*sys.argv[1:])
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
