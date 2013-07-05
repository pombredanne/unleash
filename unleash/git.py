from datetime import datetime
import os
from stat import S_ISLNK, S_ISDIR, S_ISREG
import time

from dateutil.tz import tzlocal
from dulwich.objects import S_ISGITLINK, Blob, Commit
import logbook

from .version import find_assign, replace_assign

log = logbook.Logger('git')


def add_path_to_tree(repo, tree, path, obj_mode, obj_id):
    parts = path.split('/')
    objects_to_update = []

    def _add(tree, parts):
        if len(parts) == 1:
            tree.add(parts[0], obj_mode, obj_id)
            objects_to_update.append(tree)
            return tree.id

        # there are more parts left
        subtree_name = parts.pop(0)

        # existing subtree?
        subtree_mode, subtree_id = tree[subtree_name]
        subtree = repo.object_store[subtree_id]
        # will raise KeyError if the parent directory does not exist

        # add remainder to subtree
        tree.add(subtree_name, subtree_mode, _add(subtree, parts))

        # update tree
        objects_to_update.append(tree)

    _add(tree, parts)
    return objects_to_update


def export_to_dir(repo, commit_id, output_dir):
    tree_id = repo.object_store[commit_id].tree
    export_tree(repo, tree_id, output_dir)


def export_tree(repo, tree_id, output_dir):
    # we assume output_dir exists and is empty
    if os.listdir(output_dir):
        raise ValueError('Directory %s not empty' % output_dir)

    for entry in repo.object_store[tree_id].iteritems():
        output_path = os.path.join(output_dir, entry.path)

        if S_ISGITLINK(entry.mode):
            raise ValueError('Does not support submodules')
        elif S_ISDIR(entry.mode):
            os.mkdir(output_path)  # mode parameter here is umasked, use chmod
            os.chmod(output_path, 0755)
            log.debug('created %s' % output_path)
            export_tree(repo, entry.sha, os.path.join(output_dir, output_path))
        elif S_ISLNK(entry.mode):
            log.debug('link %s' % output_path)
            os.symlink(repo.object_store[entry.sha].data, output_path)
        elif S_ISREG(entry.mode):
            with open(output_path, 'wb') as out:
                for chunk in repo.object_store[entry.sha].chunked:
                    out.write(chunk)
            log.debug('wrote %s' % output_path)
        else:
            raise ValueError('Cannot deal with mode of %s' % entry)


def prepare_commit(repo, parent_commit_id, new_version, author, message):
    objects_to_add = set()

    log.debug('Preparing new commit for version %s based on %s' % (
        new_version, parent_commit_id,
    ))
    tree = repo.object_store[repo.object_store[parent_commit_id].tree]

    # get setup.py
    setuppy_mode, setuppy_id = tree['setup.py']
    setuppy = repo.object_store[setuppy_id]

    # get __init__.py's
    pkg_name = find_assign(setuppy.data, 'name')
    log.debug('Package name is %s' % pkg_name)
    pkg_init_fn = '%s/__init__.py' % pkg_name

    try:
        (pkg_init_mode, pkg_init_id) =\
            tree.lookup_path(repo.object_store.__getitem__, pkg_init_fn)
    except KeyError:
        log.debug('Did not find %s' % pkg_init_fn)
    else:
        log.debug('Found %s' % pkg_init_fn)
        pkg_init = repo.object_store[pkg_init_id]
        release_pkg_init = Blob.from_string(
            replace_assign(pkg_init.data, '__version__', str(new_version))
        )
        objects_to_add.add(release_pkg_init)
        objects_to_add.update(
            add_path_to_tree(
                repo, tree, pkg_init_fn, pkg_init_mode, release_pkg_init.id
            ))

    release_setup = Blob.from_string(replace_assign(setuppy.data, 'version',
                                     str(new_version)))
    tree.add('setup.py', setuppy_mode, release_setup.id)

    objects_to_add.add(release_setup)
    objects_to_add.add(tree)

    now = int(time.time())
    new_commit = Commit()
    new_commit.parents = [parent_commit_id]

    new_commit.tree = tree.id

    new_commit.author = author
    new_commit.committer = author

    new_commit.commit_time = now
    new_commit.author_time = now

    now = int(time.time())
    offset = tzlocal().utcoffset(datetime.utcfromtimestamp(now))
    timezone = offset.days * 24 * 60 * 60 + offset.seconds
    new_commit.commit_timezone = timezone
    new_commit.author_timezone = timezone

    new_commit.encoding = 'utf8'
    new_commit.message = message
    objects_to_add.add(new_commit)

    # check objects
    for obj in objects_to_add:
        obj.check()

    return new_commit, tree, objects_to_add
