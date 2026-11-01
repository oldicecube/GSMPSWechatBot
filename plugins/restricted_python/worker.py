"""Child-process boundary and Windows Job Object limits for the interpreter."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys


def windows_job(process):
    class IO(ctypes.Structure):
        _fields_ = [(n,ctypes.c_ulonglong) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class BASIC(ctypes.Structure):
        _fields_ = [('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),
                    ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',wintypes.DWORD),
                    ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
    class EXT(ctypes.Structure):
        _fields_ = [('basic',BASIC),('io',IO),('process_memory',ctypes.c_size_t),
                    ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None,None)
    if not handle:
        raise OSError(ctypes.get_last_error(),'CreateJobObject failed')
    info = EXT()
    info.basic.flags = 0x100 | 0x2000 | 0x2 | 0x8  # memory, kill-on-close, CPU time, active process limit
    info.basic.process_time = 2 * 10000000
    info.basic.active = 1
    info.process_memory = 128 * 1024 * 1024
    if not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,wintypes.HANDLE(process._handle)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise OSError(error,'cannot apply sandbox process limits')
    return lambda:kernel.CloseHandle(handle)


def run(code):
    if not isinstance(code,str) or not code.strip() or len(code) > 16000:
        return {'success':False,'error':'code must contain 1–16000 characters'}
    process = None
    close = None
    try:
        # No inherited credentials, Python path, site packages, or code evaluation.
        env = {k:v for k,v in os.environ.items() if k.lower() in {'systemroot','windir'}}
        process = subprocess.Popen([sys.executable,'-I','-S',str(Path(__file__).with_name('interpreter.py'))],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,
            cwd=Path(__file__).parent,creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if os.name != 'nt':
            raise RuntimeError('This deployment requires Windows Job Object limits')
        close = windows_job(process)
        # Untrusted input is delivered only after applying the Job Object limits.
        out, _ = process.communicate(json.dumps({'code':code},ensure_ascii=False).encode('utf8'),timeout=3)
        if process.returncode or len(out) > 70000:
            return {'success':False,'error':'sandbox process exceeded resource limits or exited'}
        return json.loads(out.decode('utf8'))
    except subprocess.TimeoutExpired:
        return {'success':False,'error':'sandbox wall time limit (3 seconds)'}
    except Exception as exc:
        return {'success':False,'error':'sandbox unavailable: '+type(exc).__name__}
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
        if close:
            close()


async def run_async(code):
    task = asyncio.create_task(asyncio.to_thread(run,code))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task  # retain the concurrency slot until the child has exited
        raise
