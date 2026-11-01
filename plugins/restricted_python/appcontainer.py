"""Windows AppContainer (no capabilities) + Job Object. Fail closed elsewhere.

Only a copied read-only Python runtime and a disposable job directory receive
the container SID's ACL. No inherited handles, environment secrets or network
capabilities. The trusted parent exports only bounded regular files.
"""
import asyncio
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

from .worker import windows_job

NAME = 'WechatBot.PythonSandbox.v1'
MAX_DISK = 1024 * 1024
MAX_FILES = 24


def _acl(path, sid, mode):
    subprocess.run(['icacls', str(path), '/grant', f'*{sid}:(OI)(CI){mode}', '/Q'],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def _sid(name=NAME):
    userenv = C.WinDLL('userenv', use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [W.LPCWSTR, C.POINTER(C.c_void_p)]
    sid = C.c_void_p()
    result = userenv.CreateAppContainerProfile(name, name, name, None, 0, C.byref(sid))
    if result != 0:
        result = userenv.DeriveAppContainerSidFromAppContainerName(name, C.byref(sid))
    if result != 0:
        raise OSError(f'AppContainer profile unavailable: {result}')
    adv = C.WinDLL('advapi32', use_last_error=True)
    adv.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    text = W.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, C.byref(text)):
        raise C.WinError(C.get_last_error())
    value = text.value
    kernel = C.WinDLL('kernel32'); kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree(C.cast(text, C.c_void_p))
    return sid, value


def prepare(root):
    if os.name != 'nt':
        raise RuntimeError('Windows AppContainer is required')
    root = Path(root).resolve()
    runtime = root / ('runtime-' + sys.implementation.cache_tag)
    sid, sid_text = _sid()
    try:
        if not (runtime / '.ready').exists():
            runtime.mkdir(parents=True, exist_ok=True)
            base = Path(sys.base_prefix)
            for pattern in ('python.exe', 'python*.dll', 'vcruntime*.dll'):
                for file in base.glob(pattern):
                    shutil.copy2(file, runtime / file.name)
            dlls = runtime / 'DLLs'; dlls.mkdir(exist_ok=True)
            for file in (base / 'DLLs').iterdir():
                if file.suffix.lower() in {'.dll', '.pyd'}:
                    shutil.copy2(file, dlls / file.name)
            with zipfile.ZipFile(runtime / f'python{sys.version_info.major}{sys.version_info.minor}.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for file in (base / 'Lib').rglob('*.py'):
                    relative = file.relative_to(base / 'Lib')
                    if not set(relative.parts) & {'site-packages', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip', 'turtledemo'}:
                        archive.write(file, relative.as_posix())
            # No site, environment, registry path or host package lookup.
            (runtime / f'python{sys.version_info.major}{sys.version_info.minor}._pth').write_text(
                f'python{sys.version_info.major}{sys.version_info.minor}.zip\nDLLs\n', encoding='ascii')
            (runtime / '.ready').write_text(sys.version, encoding='utf8')
        _acl(runtime, sid_text, 'RX')
        return runtime, sid_text
    finally:
        adv = C.WinDLL('advapi32'); adv.FreeSid.argtypes = [C.c_void_p]; adv.FreeSid(sid)


def _profile_folder(sid_text):
    api = C.WinDLL('userenv')
    api.GetAppContainerFolderPath.argtypes = [W.LPCWSTR, C.POINTER(W.LPWSTR)]
    value = W.LPWSTR()
    if api.GetAppContainerFolderPath(sid_text, C.byref(value)) != 0:
        raise RuntimeError('cannot monitor AppContainer profile storage')
    path = Path(value.value)
    ole = C.WinDLL('ole32'); ole.CoTaskMemFree.argtypes = [C.c_void_p]
    ole.CoTaskMemFree(C.cast(value, C.c_void_p))
    return path


class _Process:
    """Minimal suspended Popen-compatible process for the existing job limiter."""
    def __init__(self, executable, args, directory, sid):
        class SI(C.Structure):
            _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR),
                        ('x', W.DWORD), ('y', W.DWORD), ('xs', W.DWORD), ('ys', W.DWORD),
                        ('xc', W.DWORD), ('yc', W.DWORD), ('fill', W.DWORD), ('flags', W.DWORD),
                        ('show', W.WORD), ('reserved2_size', W.WORD), ('reserved2', C.c_void_p),
                        ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)]
        class SX(C.Structure):
            _fields_ = [('si', SI), ('attrs', C.c_void_p)]
        class PI(C.Structure):
            _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE), ('pid', W.DWORD), ('tid', W.DWORD)]
        class SC(C.Structure):
            _fields_ = [('sid', C.c_void_p), ('caps', C.c_void_p), ('count', W.DWORD), ('reserved', W.DWORD)]
        k = C.WinDLL('kernel32', use_last_error=True)
        k.InitializeProcThreadAttributeList.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)]
        k.UpdateProcThreadAttribute.argtypes = [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p]
        k.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD,
                                    C.c_void_p, W.LPCWSTR, C.POINTER(SX), C.POINTER(PI)]
        size = C.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
        buf = C.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(buf, 1, 0, C.byref(size)):
            raise C.WinError(C.get_last_error())
        try:
            caps = SC(sid, None, 0, 0)
            if not k.UpdateProcThreadAttribute(buf, 0, 0x20009, C.byref(caps), C.sizeof(caps), None, None):
                raise C.WinError(C.get_last_error())
            sx = SX(); sx.si.cb = C.sizeof(sx); sx.attrs = C.cast(buf, C.c_void_p)
            pi = PI()
            command = C.create_unicode_buffer(subprocess.list2cmdline([str(executable), *args]))
            env = C.create_unicode_buffer('\0'.join(f'{key}={value}' for key, value in sorted({
                'SystemRoot': os.environ['SystemRoot'], 'WINDIR': os.environ['SystemRoot'],
                'LOCALAPPDATA': os.environ['LOCALAPPDATA'], 'USERPROFILE': os.environ['USERPROFILE'],
                'TEMP': str(directory), 'TMP': str(directory)}.items())) + '\0\0')
            if not k.CreateProcessW(str(executable), command, None, None, False,
                                   0x80000 | 0x400 | 0x4 | 0x08000000, env, str(directory), C.byref(sx), C.byref(pi)):
                raise C.WinError(C.get_last_error())
            self._handle, self.thread, self.pid = pi.process, pi.thread, pi.pid
            self.k = k
            for name in ('CloseHandle', 'ResumeThread', 'TerminateProcess', 'WaitForSingleObject', 'GetExitCodeProcess'):
                getattr(k, name).argtypes = {
                    'CloseHandle': [W.HANDLE], 'ResumeThread': [W.HANDLE],
                    'TerminateProcess': [W.HANDLE, W.UINT], 'WaitForSingleObject': [W.HANDLE, W.DWORD],
                    'GetExitCodeProcess': [W.HANDLE, C.POINTER(W.DWORD)]}[name]
        finally:
            k.DeleteProcThreadAttributeList.argtypes = [C.c_void_p]
            k.DeleteProcThreadAttributeList(buf)

    def resume(self):
        if self.k.ResumeThread(self.thread) == 0xffffffff:
            raise C.WinError(C.get_last_error())

    def poll(self):
        if self.k.WaitForSingleObject(self._handle, 0) == 258:
            return None
        value = W.DWORD(); self.k.GetExitCodeProcess(self._handle, C.byref(value))
        return value.value

    def close(self):
        if self.poll() is None:
            self.k.TerminateProcess(self._handle, 1)
        self.k.WaitForSingleObject(self._handle, 3000)
        self.k.CloseHandle(self.thread); self.k.CloseHandle(self._handle)


RUNNER = '''import sys,os,json,io,traceback
class Output(io.StringIO):
 def write(self,s):
  if self.tell()+len(s)>8000: raise RuntimeError('output limit')
  return super().write(s)
sys.stdout=sys.stderr=Output()
try:
 code=open('program.py',encoding='utf8').read()
 exec(compile(code,'program.py','exec'),{'__name__':'__main__'})
 result={'success':True,'stdout':sys.stdout.getvalue()}
except BaseException as e:
 result={'success':False,'stdout':sys.stdout.getvalue(),'error':type(e).__name__+': '+str(e)[:1000]}
with open('_result.json','w',encoding='utf8') as f:json.dump(result,f)
'''


def run_program(code, root):
    if not isinstance(code, str) or not 0 < len(code) <= 16000:
        return {'success': False, 'error': 'code length must be 1..16000'}
    process = close_job = sid = None
    job = runtime = None
    profile = sid_text = None
    try:
        runtime, _ = prepare(root)
        profile = NAME + '.' + uuid.uuid4().hex
        sid, sid_text = _sid(profile)
        profile_folder = _profile_folder(sid_text)
        _acl(runtime, sid_text, 'RX')
        job = Path(root).resolve() / ('job-' + uuid.uuid4().hex)
        job.mkdir(parents=True)
        (job / 'program.py').write_text(code, encoding='utf8')
        _acl(job, sid_text, 'M')
        process = _Process(runtime / 'python.exe', ['-I', '-S', '-c', RUNNER], job, sid)
        close_job = windows_job(process)
        process.resume()
        start = time.monotonic()
        while process.poll() is None:
            files = [*job.rglob('*'), *profile_folder.rglob('*')]
            if any(f.lstat().st_file_attributes & 0x400 for f in files):
                return {'success': False, 'error': 'sandbox reparse point denied'}
            if len(files) > MAX_FILES + 16 or sum(f.lstat().st_size for f in files if f.is_file()) > MAX_DISK:
                return {'success': False, 'error': 'sandbox disk limit'}
            if time.monotonic() - start > 5:
                return {'success': False, 'error': 'sandbox wall time limit'}
            time.sleep(.02)
        if process.poll() != 0 or not (job / '_result.json').exists():
            return {'success': False, 'error': 'sandbox resource limit or process exit', 'exit_code': process.poll()}
        files = list(job.iterdir())
        if job.lstat().st_file_attributes & 0x400 or any(f.lstat().st_file_attributes & 0x400 for f in files):
            return {'success': False, 'error': 'sandbox reparse point denied'}
        if sum(f.lstat().st_size for f in files) > MAX_DISK or len(files) > MAX_FILES:
            return {'success': False, 'error': 'sandbox disk limit'}
        result = json.loads((job / '_result.json').read_text(encoding='utf8'))
        artifacts = []
        for file in files:
            if file.name.startswith('_') or file.name == 'program.py' or file.is_symlink() or not file.is_file():
                continue
            # Export data only, never executable files/links/paths supplied by code.
            if file.suffix.lower() in {'.txt', '.json', '.csv', '.md', '.svg'} and file.stat().st_size <= 65536:
                artifacts.append({'name': file.name, 'text': file.read_text(encoding='utf8', errors='replace')[:32768]})
        result.update(isolation='windows-appcontainer', artifacts=artifacts[:8], code_sha256=hashlib.sha256(code.encode()).hexdigest())
        return result
    except Exception as exc:
        return {'success': False, 'error': 'AppContainer unavailable: ' + type(exc).__name__ + ': ' + str(exc)[:300]}
    finally:
        if process:
            process.close()
        if close_job:
            close_job()
        if sid:
            adv = C.WinDLL('advapi32'); adv.FreeSid.argtypes = [C.c_void_p]; adv.FreeSid(sid)
        if profile:
            userenv = C.WinDLL('userenv'); userenv.DeleteAppContainerProfile.argtypes = [W.LPCWSTR]
            userenv.DeleteAppContainerProfile(profile)
        if sid_text and runtime:
            subprocess.run(['icacls', str(runtime), '/remove:g', '*' + sid_text, '/Q'],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if job:
            assert job.parent == Path(root).resolve() and job.name.startswith('job-')
            shutil.rmtree(job, ignore_errors=True)


async def run_program_async(code, root):
    task = asyncio.create_task(asyncio.to_thread(run_program, code, root))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
