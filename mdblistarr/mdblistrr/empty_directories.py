"""Bounded Linux directory-only cleanup; never unlink or recursively remove.

Only an immediate title directory and its captured immediate season directories
are supported. All traversal uses no-follow directory descriptors. A concurrent
import makes rmdir fail atomically. Linux has no inode-conditional rmdir: a
concurrent replacement by another *empty* directory between the final identity
check and rmdir remains possible; file contents can never be removed here.
"""
from contextlib import contextmanager
import os
import posixpath
import stat


class DirectoryDeferred(ValueError):
    pass


FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def fingerprint(fd):
    value = os.fstat(fd)
    return {'device': value.st_dev, 'inode': value.st_ino}


def mount_identity(fd):
    with open(f'/proc/self/fdinfo/{fd}', encoding='ascii') as stream:
        mounts = [line.split()[1] for line in stream if line.startswith('mnt_id:')]
    if len(mounts) != 1:
        raise DirectoryDeferred('Directory mount identity is unavailable.')
    return {'device': os.fstat(fd).st_dev, 'mount': mounts[0]}


def canonical(path):
    return (isinstance(path, str) and path.startswith('/') and path != '/'
            and posixpath.normpath(path) == path and '\x00' not in path
            and not path.startswith('//'))


@contextmanager
def open_root(path):
    if not canonical(path):
        raise DirectoryDeferred('An explicit canonical library root is required.')
    descriptors = []
    try:
        fd = os.open('/', FLAGS)
        descriptors.append(fd)
        for component in path.split('/')[1:]:
            fd = os.open(component, FLAGS, dir_fd=fd)
            descriptors.append(fd)
        yield fd
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def same_mount(parent, child):
    if any(parent[key] != child[key] for key in ('device', 'mount')):
        raise DirectoryDeferred('A directory crosses the approved library mount.')


def checked_child(parent, name, expected=None):
    fd = os.open(name, FLAGS, dir_fd=parent)
    try:
        actual = fingerprint(fd)
        same_mount(mount_identity(parent), mount_identity(fd))
        if expected is not None and actual != expected:
            raise DirectoryDeferred('A captured directory identity changed.')
        return fd
    except BaseException:
        os.close(fd)
        raise


def snapshot(root, title, product, season_paths=()):
    if not canonical(title) or posixpath.dirname(title) != root:
        raise DirectoryDeferred('The title must be directly inside its approved root.')
    permitted = {posixpath.basename(path) for path in season_paths
                 if canonical(path) and posixpath.dirname(path) == title}
    with open_root(root) as parent:
        fd = checked_child(parent, posixpath.basename(title))
        try:
            directories = [{'name': '', 'identity': fingerprint(fd)}]
            names = os.listdir(fd)
            if len(names) > 20000:
                raise DirectoryDeferred('Title directory inventory exceeds its limit.')
            for name in sorted(names):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue  # Files/links are never followed or removed here.
                if product != 'sonarr' or name not in permitted or len(directories) >= 129:
                    raise DirectoryDeferred('Unexpected nested movie or season directories.')
                child = checked_child(fd, name)
                try:
                    children = os.listdir(child)
                    if len(children) > 20000 or any(stat.S_ISDIR(os.stat(
                            entry, dir_fd=child, follow_symlinks=False).st_mode) for entry in children):
                        raise DirectoryDeferred('Nested season directories are unsupported.')
                    directories.append({'name': name, 'identity': fingerprint(child)})
                finally:
                    os.close(child)
            return {'root': root, 'title': title, 'root_identity': fingerprint(parent),
                    'directories': directories, 'removed': [], 'intent': None}
        finally:
            os.close(fd)


@contextmanager
def locate(plan, name):
    """Reopen the original parent; absence is distinct from failed traversal."""
    if (not canonical(plan['title']) or posixpath.dirname(plan['title']) != plan['root']
            or not isinstance(name, str) or '/' in name or name in ('.', '..')
            or name not in [row['name'] for row in plan['directories']]):
        raise DirectoryDeferred('The captured directory plan is invalid.')
    with open_root(plan['root']) as root:
        if fingerprint(root) != plan['root_identity']:
            raise DirectoryDeferred('The captured library root changed.')
        title_name = posixpath.basename(plan['title'])
        title_identity = plan['directories'][0]['identity']
        if name == '':
            yield root, title_name, title_identity
            return
        title = checked_child(root, title_name, title_identity)
        try:
            expected = next(row['identity'] for row in plan['directories'] if row['name'] == name)
            yield title, name, expected
        finally:
            os.close(title)


def inspect(plan, name):
    with locate(plan, name) as (parent, leaf, expected):
        try:
            child = checked_child(parent, leaf, expected)
        except FileNotFoundError:
            return 'absent'
        try:
            return 'nonempty' if os.listdir(child) else 'empty'
        finally:
            os.close(child)


def remove(plan, name, persist_intent):
    """Intent is saved only after safe opening and emptiness checks, before rmdir."""
    with locate(plan, name) as (parent, leaf, expected):
        child = checked_child(parent, leaf, expected)
        try:
            if os.listdir(child):
                raise DirectoryDeferred('Directory has remaining contents; nothing was removed.')
            persist_intent()
            # Pin and recheck both descriptors and the name immediately before
            # the sole mutation. rmdir itself enforces emptiness at syscall time.
            if fingerprint(child) != expected:
                raise DirectoryDeferred('Directory changed before removal.')
            current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
                    expected['device'], expected['inode']):
                raise DirectoryDeferred('Directory name was replaced before removal.')
            os.rmdir(leaf, dir_fd=parent)
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise DirectoryDeferred('Directory disappearance could not be verified.')
        finally:
            os.close(child)
