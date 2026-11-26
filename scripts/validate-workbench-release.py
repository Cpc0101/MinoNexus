"""Validate complete static release contents against its immutable source identity."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile


def identity(payload, version, commit):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid expected release identity')
    if (payload.get('version'), payload.get('source_commit'), payload.get('build_id')) != (version, commit, f'workbench-{version}-{commit}'):
        raise ValueError('Release identity does not match expected tag commit')


def validate(root, version, commit):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Unsafe release directory')
    files = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlink in static release')
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError('Nonregular static asset')
        files[path.relative_to(root).as_posix()] = path
    payload = json.loads(files['BUILD.json'].read_text())
    identity(payload, version, commit)
    manifest = payload.get('sha256')
    if not isinstance(manifest, dict) or 'index.html' not in manifest or set(manifest) != set(files) - {'BUILD.json'}:
        raise ValueError('Incomplete or extra static release files')
    for name, digest in manifest.items():
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or hashlib.sha256(files[name].read_bytes()).hexdigest() != digest:
            raise ValueError(f'Static asset hash mismatch: {name}')
    return payload


def extract(package, output):
    output = Path(output)
    if not output.is_dir() or any(output.iterdir()):
        raise ValueError('Extraction directory must be empty')
    with tarfile.open(package, 'r:gz') as archive:
        members = []
        seen = set()
        for row in archive.getmembers():
            name = row.name.removeprefix('./').rstrip('/')
            if not name or name == '.':
                if row.isdir():
                    continue
                raise ValueError('Invalid archive root')
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name or name in seen:
                raise ValueError('Unsafe or duplicate archive path')
            if not (row.isfile() or row.isdir()):
                raise ValueError('Archive links and special files are forbidden')
            seen.add(name)
            members.append((row, path))
        output.chmod(0o755)
        for row, path in members:
            target = output.joinpath(*path.parts)
            if row.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(row) as source, target.open('xb') as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
                target.chmod(0o644)
        for directory in output.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)


if __name__ == '__main__':
    try:
        mode, source, version, commit, *extra = sys.argv[1:]
        if mode == 'package':
            extract(source, extra[0])
            validate(extra[0], version, commit)
        elif mode == 'directory':
            validate(source, version, commit)
        elif mode == 'published':
            actual = json.loads(Path(source).read_text())
            expected = json.loads(Path(extra[0]).read_text())
            identity(actual, version, commit)
            if actual != expected:
                raise ValueError('Published BUILD.json differs from verified package')
        else:
            raise ValueError('Unknown validation mode')
        print('Verified complete Workbench release identity and metadata')
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        raise SystemExit(str(error)) from None
