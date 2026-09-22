"""Send a verified package to Wenlu and wait for its persisted import acknowledgement."""
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

from .core import inspect_package
from .embedding import check_cancel


def data_directory():
    return Path(os.environ.get('WENLU_DATA_DIR', str(Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Wenlu')))


def launcher_command(target):
    target = Path(target).resolve(strict=True)
    if target.name.lower() == 'wenlu.exe':
        return [str(target)]
    if target.name == 'launcher.py' and (target.parent / 'knowledge_import.py').is_file():
        python = target.parent / '.venv' / 'Scripts' / 'pythonw.exe'
        if python.is_file():
            return [str(python), str(target)]
    raise ValueError('闻录启动路径无效，请先启动一次支持一键导入的新版闻录。')


def find_launcher(data):
    candidates = []
    try:
        value = json.loads((Path(data) / 'knowledge-launcher.json').read_text(encoding='utf-8'))
        if value.get('protocol') == 1 and isinstance(value.get('target'), str):
            candidates.append(Path(value['target']))
    except (OSError, ValueError, AttributeError):
        pass
    if os.name == 'nt':
        import winreg
        key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{9D469B55-B7E6-497F-89B4-FCD21C0929C1}_is1'
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, key) as registry:
                    folder, _ = winreg.QueryValueEx(registry, 'InstallLocation')
                    candidates.append(Path(folder) / 'Wenlu.exe')
            except OSError:
                pass
    candidates.append(Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Programs' / 'Wenlu' / 'Wenlu.exe')
    for target in candidates:
        try:
            return launcher_command(target)
        except (OSError, ValueError):
            continue
    raise ValueError('未找到闻录。请先启动一次支持一键导入的新版闻录，然后重试。')


def live_endpoint(client, data):
    try:
        value = json.loads((Path(data) / 'knowledge-import.json').read_text(encoding='utf-8'))
        if value.get('protocol') != 1 or type(value.get('port')) is not int or not 1024 <= value['port'] <= 65535:
            return None
        token = value.get('token')
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        url = f'http://127.0.0.1:{value["port"]}'
        headers = {'Authorization': 'Bearer ' + token}
        response = client.get(url + '/health', headers=headers, timeout=.7)
        response.raise_for_status()
        if response.json() != {'ok': True, 'protocol': 1}:
            return None
        return url, headers
    except (OSError, ValueError, AttributeError, httpx.HTTPError):
        return None


def import_package(package, cancel=None, progress=print, data=None):
    path = Path(package).resolve(strict=True)
    if path.suffix.lower() != '.wlkb':
        raise ValueError('请选择已生成的 .wlkb 知识包。')
    inspect_package(path)
    check_cancel(cancel)
    data = Path(data) if data is not None else data_directory()
    # Local handoff never uses network proxies or a remote endpoint supplied by metadata.
    with httpx.Client(trust_env=False, timeout=35) as client:
        endpoint = live_endpoint(client, data)
        if endpoint is None:
            command = find_launcher(data)
            progress('正在启动闻录…')
            process = subprocess.Popen(command, cwd=str(Path(command[-1]).parent),
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                check_cancel(cancel)
                endpoint = live_endpoint(client, data)
                if endpoint is not None:
                    break
                exit_code = process.poll()
                if exit_code is not None:
                    raise ValueError('闻录启动未就绪。若旧版闻录正在运行，请先正常退出，再点击导入。'
                                     if exit_code == 0 else '闻录启动失败，请手动打开闻录查看错误。')
                if cancel is not None:
                    cancel.wait(.25)
                else:
                    time.sleep(.25)
            if endpoint is None:
                raise ValueError('闻录未响应导入。若已打开旧版，请正常退出后更新到支持一键导入的版本再重试。')
        check_cancel(cancel)
        progress('正在导入闻录…')
        url, headers = endpoint
        try:
            response = client.post(url + '/import', headers=headers, json={'path': str(path)})
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError('未收到闻录的导入确认，请在闻录中检查知识包列表后重试。') from exc
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError(result.get('error', '闻录未完成导入。') if isinstance(result, dict) else '闻录导入响应无效。')
        if not isinstance(result.get('path'), str) or not isinstance(result.get('name'), str):
            raise ValueError('闻录导入确认格式无效，请检查知识包列表。')
        return result
