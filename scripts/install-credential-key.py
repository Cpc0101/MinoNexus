"""Validate an existing master key or atomically create it without replacement."""
import base64
import fcntl
import hmac
import os
from pathlib import Path
import stat
import sys
import tempfile


def decode(value):
    value = value.strip()
    try:
        raw = base64.b64decode(value + b'=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except ValueError:
        raise ValueError('Invalid master key encoding') from None
    if len(raw) != 32:
        raise ValueError('Master key must decode to 32 bytes')
    return raw


def install(directory, value, uid=10001, gid=10001):
    raw = decode(value)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Unsafe credential directory')
    info = directory.stat()
    if info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError('Credential directory must be private and owned by installer')
    lock = os.open(directory / '.install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = directory / 'credential-master.key'
        if os.path.lexists(target):
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != uid or info.st_gid != gid:
                    raise ValueError('Unsafe existing master key ownership or permissions')
                if not hmac.compare_digest(decode(stream.read(4096)), raw):
                    raise ValueError('Existing master key differs; explicit rotation required')
            return 'validated existing key; unchanged'
        fd, temporary = tempfile.mkstemp(prefix='.credential-master-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(base64.urlsafe_b64encode(raw) + b'\n')
                stream.flush()
                os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
            os.link(temporary, target, follow_symlinks=False)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            os.unlink(temporary)
        return 'atomically installed new key'
    finally:
        os.close(lock)


if __name__ == '__main__':
    try:
        print(install(sys.argv[1], sys.stdin.buffer.read(4096)))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
