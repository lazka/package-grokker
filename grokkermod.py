import concurrent.futures
import os
import pefile
import tarfile
import threading
import zstandard

from contextlib import contextmanager, closing
from urllib.request import urlopen

from pacdb import pacdb

_tls = threading.local()

PE_FILE_EXTENSIONS = frozenset((".dll", ".exe", ".pyd"))


@contextmanager
def open_zstd_supporting_tar(name, fileobj):
    # HACK: please, Python, support zst with |* in tarfile
    # could probably check for magic, but would have to have a stream wrapper
    # like tarfile already has to "put back" the magic bytes
    if name.endswith(".zst"):
        if not hasattr(_tls, 'zdctx'):
            _tls.zdctx = zstandard.ZstdDecompressor()
        with _tls.zdctx.stream_reader(fileobj, closefd=False) as zstream, \
             tarfile.open(fileobj=zstream, mode="r|") as tar:
            yield tar
    else:
        with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
            yield tar


class ProblematicImportSearcher(object):
    def __init__(self, problem_dll_symbols, local_mirror=None):
        super(ProblematicImportSearcher, self).__init__()
        self.problem_dlls = problem_dll_symbols
        self.local_mirror = local_mirror

    def _open_package(self, pkg):
        if self.local_mirror:
            localfile = os.path.join(self.local_mirror, pkg.filename)
            return open(localfile, "rb")
        else:
            return urlopen("{}/{}".format(pkg.db.url, pkg.filename))

    def __call__(self, pkg):
        if not any(os.path.splitext(f)[-1] in PE_FILE_EXTENSIONS for f in pkg.files):
            return None
        with self._open_package(pkg) as pkgfile, \
             open_zstd_supporting_tar(pkg.filename, pkgfile) as tar:
            for entry in tar:
                if not entry.isreg() or os.path.splitext(entry.name)[-1] not in PE_FILE_EXTENSIONS:
                    continue

                try:
                    with tar.extractfile(entry) as infofile, \
                         closing(pefile.PE(data=infofile.read(), fast_load=True)) as pe:
                        pe.parse_data_directories(directories=[
                            pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']
                        ])
                        for entry in pe.DIRECTORY_ENTRY_IMPORT:
                            problem_symbols = self.problem_dlls.get(entry.dll.lower(), None)
                            if problem_symbols is not None:
                                if not problem_symbols:
                                    return pkg
                                for imp in entry.imports:
                                    if imp.name in problem_symbols:
                                        return pkg
                except pefile.PEFormatError:
                    continue
        return None


def grok_dependency_tree(repo, package, package_handler):
    with concurrent.futures.ThreadPoolExecutor(20) as executor:
        done={}
        todo=[package]

        # Check packages that immediately makedepend on the given package
        # https://github.com/jeremyd2019/package-grokker/issues/6
        for pkgname in repo.get_pkg(package).compute_rdepends('makedepends'):
            done[pkgname] = executor.submit(package_handler, repo.get_pkg(pkgname))

        while todo:
            more=[]
            for pkgname in todo:
                pkg = repo.get_pkg(pkgname)
                more.extend(rdep for rdep in pkg.compute_requiredby() if rdep not in done)
                done[pkgname] = executor.submit(package_handler, pkg)
            todo = more

        del repo

        for future in concurrent.futures.as_completed(done.values()):
            result = future.result()
            if result is not None:
                yield result.base
