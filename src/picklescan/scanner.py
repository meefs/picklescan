from dataclasses import dataclass
from enum import Enum
import http.client
import io
import json
import logging
import os
import pickletools
from tarfile import TarError
from tempfile import TemporaryDirectory
from typing import IO, List, Optional, Set, Tuple
import urllib.parse
import zipfile
from .relaxed_zipfile import RelaxedZipFile

from .torch import (
    get_magic_number,
    InvalidMagicError,
    _is_zipfile,
    MAGIC_NUMBER,
    _should_read_directly,
)


class SafetyLevel(Enum):
    Innocuous = "innocuous"
    Suspicious = "suspicious"
    Dangerous = "dangerous"


@dataclass
class Global:
    module: str
    name: str
    safety: SafetyLevel


@dataclass
class ScanResult:
    globals: List[Global]
    scanned_files: int = 0
    issues_count: int = 0
    infected_files: int = 0
    scan_err: bool = False

    def merge(self, sr: "ScanResult"):
        self.globals.extend(sr.globals)
        self.scanned_files += sr.scanned_files
        self.issues_count += sr.issues_count
        self.infected_files += sr.infected_files
        self.scan_err = self.scan_err or sr.scan_err


class GenOpsError(Exception):
    def __init__(self, msg: str, globals: Optional[Set[Tuple[str, str]]]):
        self.msg = msg
        self.globals = globals
        super().__init__()

    def __str__(self) -> str:
        return self.msg


_log = logging.getLogger("picklescan")

_safe_globals = {
    "collections": {"OrderedDict"},
    "torch": {
        "LongStorage",
        "FloatStorage",
        "HalfStorage",
        "QUInt2x4Storage",
        "QUInt4x2Storage",
        "QInt32Storage",
        "QInt8Storage",
        "QUInt8Storage",
        "ComplexFloatStorage",
        "ComplexDoubleStorage",
        "DoubleStorage",
        "BFloat16Storage",
        "BoolStorage",
        "CharStorage",
        "ShortStorage",
        "IntStorage",
        "ByteStorage",
    },
    "numpy": {
        "dtype",
        "ndarray",
    },
    "numpy._core.multiarray": {
        "_reconstruct",
    },
    "numpy.core.multiarray": {
        "_reconstruct",
    },
    "torch._utils": {"_rebuild_tensor_v2"},
}

_unsafe_globals = {
    "__builtin__": {
        "eval",
        "compile",
        "getattr",
        "apply",
        "exec",
        "open",
        "breakpoint",
    },  # Pickle versions 0, 1, 2 have those function under '__builtin__'
    "builtins": {
        "eval",
        "compile",
        "getattr",
        "apply",
        "exec",
        "open",
        "breakpoint",
    },  # Pickle versions 3, 4 have those function under 'builtins'
    "aiohttp.client": "*",
    "asyncio": "*",
    "bdb": "*",
    "commands": "*",  # Python 2 precursor to subprocess
    "functools": "partial",  # functools.partial(os.system, "echo pwned")
    "httplib": "*",  # Includes http.client.HTTPSConnection()
    "numpy.testing._private.utils": "*",  # runstring() in this module is a synonym for exec()
    "nt": "*",  # Alias for 'os' on Windows. Includes os.system()
    "posix": "*",  # Alias for 'os' on Linux. Includes os.system()
    "operator": "attrgetter",  # Ex of code execution: operator.attrgetter("system")(__import__("os"))("echo pwned")
    "os": "*",
    "requests.api": "*",
    "runpy": "*",  # Includes runpy._run_code
    "shutil": "*",
    "socket": "*",
    "ssl": "*",  # DNS exfiltration via ssl.get_server_certificate()
    "subprocess": "*",
    "sys": "*",
    "pdb": "*",
    "pickle": "*",
    "_pickle": "*",
    "pip": "*",
    "pydoc": "pipepager",  # pydoc.pipepager('help','echo pwned')
    "timeit": "*",
    "torch._inductor.codecache": "compile_file",  # compile_file('', '', ['sh', '-c','$(echo pwned)'])
    "torch.serialization": "load",  # pickle could be used to load a different file
    "venv": "*",
    "webbrowser": "*",  # Includes webbrowser.open()
}

#
# TODO: handle methods loading other Pickle files (either mark as suspicious, or follow calls to scan other files [preventing infinite loops])
#
# numpy.load()
# https://numpy.org/doc/stable/reference/generated/numpy.load.html#numpy.load
# numpy.ctypeslib.load_library()
# https://numpy.org/doc/stable/reference/routines.ctypeslib.html#numpy.ctypeslib.load_library
# pandas.read_pickle()
# https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_pickle.html
# joblib.load()
# https://joblib.readthedocs.io/en/latest/generated/joblib.load.html
# torch.load()
# https://pytorch.org/docs/stable/generated/torch.load.html
# tf.keras.models.load_model()
# https://www.tensorflow.org/api_docs/python/tf/keras/models/load_model
#

_numpy_file_extensions = {".npy"}  # Note: .npz is handled as zip files
_pytorch_file_extensions = {".bin", ".pt", ".pth", ".ckpt"}
_pickle_file_extensions = {".pkl", ".pickle", ".joblib", ".dat", ".data"}
_zip_file_extensions = {".zip", ".npz", ".7z"}
# Pickle files do not actually have magic bytes, but v2+ files
# start with a PROTO (\x80) opcode followed by a byte with the protocol version
_pickle_magic_bytes = {
    b"\x80\x00",
    b"\x80\x01",
    b"\x80\x02",
    b"\x80\x03",
    b"\x80\x04",
    b"\x80\x05",
}
_numpy_magic_bytes = b"\x93NUMPY"


def _is_7z_file(f: IO[bytes]) -> bool:
    read_bytes = []
    start = f.tell()

    byte = f.read(1)
    while byte != b"":
        read_bytes.append(byte)
        if len(read_bytes) == 6:
            break
        byte = f.read(1)
    f.seek(start)

    local_header_magic_number = [b"7", b"z", b"\xbc", b"\xaf", b"\x27", b"\x1c"]
    return read_bytes == local_header_magic_number


def _http_get(url) -> bytes:
    _log.debug(f"Request: GET {url}")

    parsed_url = urllib.parse.urlparse(url)
    path_and_query = parsed_url.path + (
        "?" + parsed_url.query if len(parsed_url.query) > 0 else ""
    )

    conn = http.client.HTTPSConnection(parsed_url.netloc)
    try:
        conn.request("GET", path_and_query)
        response = conn.getresponse()
        _log.debug(f"Response: status code {response.status} reason {response.reason}")
        if response.status == 302:  # Follow redirections
            return _http_get(response.headers["Location"])
        elif response.status >= 400:
            raise RuntimeError(
                f"HTTP {response.status} ({response.reason}) calling GET {parsed_url.scheme}://{parsed_url.netloc}{path_and_query}"
            )
        return response.read()
    finally:
        conn.close()


def _list_globals(data: IO[bytes], multiple_pickles=True) -> Set[Tuple[str, str]]:
    globals = set()

    memo = {}
    # Scan the data for pickle buffers, stopping when parsing fails or stops making progress
    last_byte = b"dummy"
    parsing_pkl_error = None
    while last_byte != b"":
        # List opcodes
        ops = []
        try:
            for op in pickletools.genops(data):
                ops.append(op)
        except Exception as e:
            parsing_pkl_error = str(e)
        last_byte = data.read(1)
        data.seek(-1, 1)

        # Extract global imports
        for n in range(len(ops)):
            op = ops[n]
            op_name = op[0].name
            op_value = op[1]

            if op_name == "MEMOIZE" and n > 0:
                memo[len(memo)] = ops[n - 1][1]
            elif op_name in ["PUT", "BINPUT", "LONG_BINPUT"] and n > 0:
                memo[op_value] = ops[n - 1][1]
            elif op_name in ("GLOBAL", "INST"):
                globals.add(tuple(op_value.split(" ", 1)))
            elif op_name == "STACK_GLOBAL":
                values = []
                for offset in range(1, n):
                    if ops[n - offset][0].name in [
                        "MEMOIZE",
                        "PUT",
                        "BINPUT",
                        "LONG_BINPUT",
                    ]:
                        continue
                    if ops[n - offset][0].name in ["GET", "BINGET", "LONG_BINGET"]:
                        values.append(memo[int(ops[n - offset][1])])
                    elif ops[n - offset][0].name not in [
                        "SHORT_BINUNICODE",
                        "UNICODE",
                        "BINUNICODE",
                        "BINUNICODE8",
                    ]:
                        _log.debug(
                            "Presence of non-string opcode, categorizing as an unknown dangerous import"
                        )
                        values.append("unknown")
                    else:
                        values.append(ops[n - offset][1])
                    if len(values) == 2:
                        break
                if len(values) != 2:
                    raise ValueError(
                        f"Found {len(values)} values for STACK_GLOBAL at position {n} instead of 2."
                    )
                globals.add((values[1], values[0]))

        if not multiple_pickles:
            break

        if parsing_pkl_error is not None:
            # XXX: given we can have multiple pickles in a file, we may have already successfully extracted globals from a valid pickle.
            # Thus return the already found globals in the error & let the caller decide what to do.
            # Additionally, we return the error at the end of the loop to scan imports in partially broken files,
            # which can unpickle and be dangerous regardless of being valid pickle.
            globals_opt = globals if len(globals) > 0 else None
            raise GenOpsError(parsing_pkl_error, globals_opt)

    return globals


def _build_scan_result_from_raw_globals(
    raw_globals: Set[Tuple[str, str]],
    file_id,
    scan_err=False,
) -> ScanResult:
    globals = []
    issues_count = 0
    for rg in raw_globals:
        g = Global(rg[0], rg[1], SafetyLevel.Dangerous)
        safe_filter = _safe_globals.get(g.module)
        unsafe_filter = _unsafe_globals.get(g.module)
        if "unknown" in g.module or "unknown" in g.name:
            g.safety = SafetyLevel.Dangerous
            _log.warning(
                "%s: %s import '%s %s' FOUND", file_id, g.safety.value, g.module, g.name
            )
            issues_count += 1
        elif unsafe_filter is not None and (
            unsafe_filter == "*" or g.name in unsafe_filter
        ):
            g.safety = SafetyLevel.Dangerous
            _log.warning(
                "%s: %s import '%s %s' FOUND", file_id, g.safety.value, g.module, g.name
            )
            issues_count += 1
        elif safe_filter is not None and (safe_filter == "*" or g.name in safe_filter):
            g.safety = SafetyLevel.Innocuous
        else:
            g.safety = SafetyLevel.Suspicious
        globals.append(g)

    return ScanResult(globals, 1, issues_count, 1 if issues_count > 0 else 0, scan_err)


def scan_pickle_bytes(data: IO[bytes], file_id, multiple_pickles=True) -> ScanResult:
    """Disassemble a Pickle stream and report issues"""

    try:
        raw_globals = _list_globals(data, multiple_pickles)
    except GenOpsError as e:
        _log.error(f"ERROR: parsing pickle in {file_id}: {e}")
        if e.globals is not None:
            return _build_scan_result_from_raw_globals(
                e.globals, file_id, scan_err=True
            )
        else:
            return ScanResult([], scan_err=True)

    _log.debug("Global imports in %s: %s", file_id, raw_globals)

    return _build_scan_result_from_raw_globals(raw_globals, file_id)


# XXX: it appears there is not way to get the byte stream for a given file within the 7z archive and thus forcing us to unzip to disk before scanning
def scan_7z_bytes(data: IO[bytes], file_id) -> ScanResult:
    try:
        import py7zr
    except ImportError:
        raise Exception(
            "py7zr is required to scan 7z archives, install picklescan using: 'pip install picklescan[7z]'"
        )
    result = ScanResult([])

    with py7zr.SevenZipFile(data, mode="r") as archive:
        file_names = archive.getnames()
        targets = [f for f in file_names if f.endswith(tuple(_pickle_file_extensions))]
        _log.debug("Files in 7z archive %s: %s", file_id, targets)
        with TemporaryDirectory() as tmpdir:
            archive.extract(path=tmpdir, targets=targets)
            for file_name in targets:
                file_path = os.path.join(tmpdir, file_name)
                _log.debug("Scanning file %s in 7z archive %s", file_name, file_id)
                if os.path.isfile(file_path):
                    result.merge(scan_file_path(file_path))

            return result


def scan_zip_bytes(data: IO[bytes], file_id) -> ScanResult:
    result = ScanResult([])

    with RelaxedZipFile(data, "r") as zip:
        file_names = zip.namelist()
        _log.debug("Files in zip archive %s: %s", file_id, file_names)
        for file_name in file_names:
            try:
                with zip.open(file_name, "r") as file:
                    magic_bytes = file.read(8)
                file_ext = os.path.splitext(file_name)[1]

                if file_ext in _pickle_file_extensions or any(
                    magic_bytes.startswith(mn) for mn in _pickle_magic_bytes
                ):
                    _log.debug("Scanning file %s in zip archive %s", file_name, file_id)
                    with zip.open(file_name, "r") as file:
                        result.merge(scan_pickle_bytes(file, f"{file_id}:{file_name}"))

                elif file_ext in _numpy_file_extensions or magic_bytes.startswith(
                    _numpy_magic_bytes
                ):
                    _log.debug("Scanning file %s in zip archive %s", file_name, file_id)
                    with zip.open(file_name, "r") as file:
                        result.merge(scan_numpy(file, f"{file_id}:{file_name}"))
            except (zipfile.BadZipFile, RuntimeError) as e:
                # Log decompression issues (password protected, corrupted, etc.)
                _log.warning(
                    "Invalid file %s in zip archive %s: %s", file_name, file_id, str(e)
                )

    return result


def scan_numpy(data: IO[bytes], file_id) -> ScanResult:
    # Delay import to avoid dependency on NumPy
    import numpy as np

    # Code to distinguish from NumPy binary files and pickles.
    _ZIP_PREFIX = b"PK\x03\x04"
    _ZIP_SUFFIX = b"PK\x05\x06"  # empty zip files start with this
    N = len(np.lib.format.MAGIC_PREFIX)
    magic = data.read(N)
    # If the file size is less than N, we need to make sure not
    # to seek past the beginning of the file
    data.seek(-min(N, len(magic)), 1)  # back-up
    if magic.startswith(_ZIP_PREFIX) or magic.startswith(_ZIP_SUFFIX):
        # .npz file
        raise ValueError(f".npz file not handled as zip file: {file_id}")
    elif magic == np.lib.format.MAGIC_PREFIX:
        # .npy file

        version = np.lib.format.read_magic(data)
        np.lib.format._check_version(version)
        _, _, dtype = np.lib.format._read_array_header(data, version)

        if dtype.hasobject:
            return scan_pickle_bytes(data, file_id)
        else:
            return ScanResult([], 1)
    else:
        return scan_pickle_bytes(data, file_id)


def scan_pytorch(data: IO[bytes], file_id) -> ScanResult:
    # new pytorch format
    if _is_zipfile(data):
        return scan_zip_bytes(data, file_id)
    elif _is_7z_file(data):
        return scan_7z_bytes(data, file_id)
    # old pytorch format
    else:
        scan_result = ScanResult([])
        should_read_directly = _should_read_directly(data)
        if should_read_directly and data.tell() == 0:
            # try loading from tar
            try:
                # TODO: implement loading from tar
                raise TarError()
            except TarError:
                # file does not contain a tar
                data.seek(0)

        magic = get_magic_number(data)
        if magic != MAGIC_NUMBER:
            raise InvalidMagicError(magic, MAGIC_NUMBER, file_id)
        for _ in range(5):
            scan_result.merge(scan_pickle_bytes(data, file_id, multiple_pickles=False))
        scan_result.scanned_files = 1
        return scan_result


def scan_bytes(data: IO[bytes], file_id, file_ext: Optional[str] = None) -> ScanResult:
    if file_ext is not None and file_ext in _pytorch_file_extensions:
        try:
            return scan_pytorch(data, file_id)
        except InvalidMagicError as e:
            _log.error(f"ERROR: Invalid magic number for file {e}")
            return ScanResult([], scan_err=True)
    elif file_ext is not None and file_ext in _numpy_file_extensions:
        return scan_numpy(data, file_id)
    else:
        is_zip = zipfile.is_zipfile(data)
        data.seek(0)
        if is_zip:
            return scan_zip_bytes(data, file_id)
        elif _is_7z_file(data):
            return scan_7z_bytes(data, file_id)
        else:
            return scan_pickle_bytes(data, file_id)


def scan_huggingface_model(repo_id):
    # List model files
    model = json.loads(
        _http_get(f"https://huggingface.co/api/models/{repo_id}").decode("utf-8")
    )
    file_names = [
        file_name
        for file_name in (sibling.get("rfilename") for sibling in model["siblings"])
        if file_name is not None
    ]

    # Scan model files
    scan_result = ScanResult([])
    for file_name in file_names:
        file_ext = os.path.splitext(file_name)[1]
        if (
            file_ext not in _zip_file_extensions
            and file_ext not in _pickle_file_extensions
            and file_ext not in _pytorch_file_extensions
        ):
            continue
        _log.debug("Scanning file %s in model %s", file_name, repo_id)
        url = f"https://huggingface.co/{repo_id}/resolve/main/{file_name}"
        data = io.BytesIO(_http_get(url))
        scan_result.merge(scan_bytes(data, url, file_ext))

    return scan_result


def scan_directory_path(path) -> ScanResult:
    scan_result = ScanResult([])

    for base_path, _, file_names in os.walk(path):
        for file_name in file_names:
            file_ext = os.path.splitext(file_name)[1]
            if (
                file_ext not in _zip_file_extensions
                and file_ext not in _pickle_file_extensions
                and file_ext not in _pytorch_file_extensions
            ):
                continue
            file_path = os.path.join(base_path, file_name)
            _log.debug("Scanning file %s", file_path)
            with open(file_path, "rb") as file:
                scan_result.merge(scan_bytes(file, file_path, file_ext))

    return scan_result


def scan_file_path(path) -> ScanResult:
    file_ext = os.path.splitext(path)[1]
    with open(path, "rb") as file:
        return scan_bytes(file, path, file_ext)


def scan_url(url) -> ScanResult:
    return scan_bytes(io.BytesIO(_http_get(url)), url)
