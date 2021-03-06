#!/usr/bin/env python3

import argparse
import asyncio
import os
import sys
import subprocess
import pathlib
import shutil

from macpack import dependency


class PatchError(Exception):
    pass


async def collect(root_dep):
    failed_paths = []
    all_resolved = []
    stack = [[root_dep]]

    while len(stack) > 0:
        # pop one item from stack, and collect all its dependencies
        current_items = stack.pop()
        all_resolved += current_items
        current_deps_and_fails = await asyncio.gather(*[d.find_dependencies() for d in current_items])

        for _, fails in current_deps_and_fails:
            failed_paths += fails
        current_items_deps = [deps_and_fail[0] for deps_and_fail in current_deps_and_fails]

        to_resolve = []
        for item, item_deps in zip(current_items, current_items_deps):

            # for every dep of the current item, try to see if we already resolved it before. If not, we
            # still have to resolve it -> push it onto the stack
            for dep in item_deps:
                if not dep.is_sys():
                    # check if we have seen that dependency before
                    if dep in all_resolved:
                        existing_dep = all_resolved[all_resolved.index(dep)]
                    elif dep in to_resolve:
                        existing_dep = to_resolve[to_resolve.index(dep)]
                    else:
                        existing_dep = None

                    if existing_dep:
                        # if we have already seen it before, merge the references into
                        # the existing dependency, and assign that one as the items dependency
                        existing_dep.merge(dep)
                        item.dependencies[item.dependencies.index(dep)] = existing_dep
                    else:
                        to_resolve.append(dep)

        if len(to_resolve) > 0:
            stack.append(to_resolve)

    if len(failed_paths) > 0:
        print('Some of the paths in the dependency tree could not be resolved', file=sys.stderr)
        print('Maybe you already bundled {}?'.format(root_dep.path.name), file=sys.stderr)
        if args.verbose:
            for path in failed_paths:
                print('Could not resolve {}'.format(path), file=sys.stderr)
        else:
            print('Run with -v to see failed paths', file=sys.stderr)


def ensure_dir(path):
    if not os.path.exists(str(path)):
        os.makedirs(str(path))


async def patch(root_item, dest_path, root_loader_path):
    items = [root_item] + root_item.get_dependencies()

    ensure_dir(dest_path)

    process_results = []

    for item in items:
        loader_path = pathlib.PurePath('@loader_path')

        if item == root_item:
            pargs = ['install_name_tool', str(root_item.path)]
        else:
            shutil.copyfile(str(item.path), str(dest_path / item.path.name))
            pargs = ['install_name_tool', str(dest_path / item.path.name)]

        pargs += ['-id', str(loader_path / item.path.name)]

        for dep in item.get_direct_dependencies():
            for reference in dep.referred_as:
                new_path = loader_path if item != root_item else root_loader_path
                new_path = new_path if dep != root_item else loader_path
                pargs += ['-change', reference, str(new_path / dep.path.name)]

        process = await asyncio.create_subprocess_exec(*pargs,
                                                            stdout=subprocess.PIPE,
                                                            stderr=subprocess.PIPE
                                                            )
        out, err = await process.communicate()
        process_results.append((process.returncode, out, err))

    did_error = False
    for (returncode, out, err), dep in zip(process_results, items):
        if returncode:
            did_error = True
            print('Error patching {}'.format(str(dep.path.name)), file=sys.stderr)
            if args.verbose:
                print(err.decode('utf-8'))

    if did_error:
        raise PatchError('One or more dependencies could not be patched')


def print_deps_minimal(d):
    deps = d.get_dependencies()

    print(str(len(deps)) + ' total non-system dependenc{}'.format('y' if len(deps) == 1 else 'ies'))

    for i, dep in enumerate(deps):
        dep_slots = [str(deps.index(d) + 1) for d in dep.get_direct_dependencies()]
        s = ', '.join(dep_slots) if len(dep_slots) > 0 else 'No dependencies'
        print(str(i + 1) + '\t' + dep.path.name + ' -> ' + s)


def print_deps(d):
    deps = d.get_dependencies()

    print(str(len(deps)) + ' total non-system dependenc{}'.format('y' if len(deps) == 1 else 'ies'))

    for dep in deps:
        print(dep.path.name)
        for dep_dep in dep.get_dependencies():
            print('-> ' + dep_dep.path.name)


def prepatch_output(d):
    print("Patching {}".format(str(args.file)))

    if args.verbose:
        print_deps(d)
    else:
        print_deps_minimal(d)


def get_dest_and_loader_path(root_dep_path, dest_path):
    if dest_path.is_absolute():
        loader_path = dest_path
    else:
        dest_path = root_dep_path.parent / dest_path
        rel_to_binary = os.path.relpath(str(dest_path), str(root_dep_path.parent))
        loader_path = pathlib.PurePath('@loader_path', rel_to_binary)

    return (dest_path, loader_path)


def main():
    try:
        d = dependency.Dependency(args.file, pathlib.PosixPath(args.file).resolve(strict=True))
    except FileNotFoundError:
        print('{} does not exist!'.format(str(args.file)), file=sys.stderr)
        sys.exit(1)

    loop = asyncio.get_event_loop()

    loop.run_until_complete(collect(d))

    dest_path, root_loader_path = get_dest_and_loader_path(d.path, args.destination)

    prepatch_output(d)

    if not args.dry_run:
        try:
            loop.run_until_complete(patch(d, dest_path, root_loader_path))
        except PatchError:  # the error should have been already printed here
            if not args.verbose: print('Run with -v for more information', file=sys.stderr)
            sys.exit(1)

        n_deps = len(d.get_dependencies())
        print()
        print('{} + {} dependenc{} successfully patched'.format(args.file.name, n_deps, 'y' if n_deps == 1 else 'ies'))

    loop.close()


parser = argparse.ArgumentParser(
    description='Copies non-system libraries used by your executable and patches them to work as a standalone bundle')
parser.add_argument('file', help='file to patch (the root, main binary)', type=pathlib.PurePath)
parser.add_argument('-v', '--verbose', help='displays more library information and output of install_name_tool',
                    action='store_true')
parser.add_argument('-n', '--dry-run', help='just show the dependency tree but do not do any patching',
                    action='store_true')
parser.add_argument('-d', '--destination', help='destination directory where the binaries will be placed and loaded',
                    type=pathlib.Path, default='../libs')
args = parser.parse_args()

# Allow execution of this file directly (it's executed differently when installed)
if __name__ == '__main__': main()
