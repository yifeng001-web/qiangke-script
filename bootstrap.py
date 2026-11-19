"""首次运行创建独立环境；不更改全局 Python 包。"""
from pathlib import Path
import os
import struct
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 10) or struct.calcsize('P') != 8:
        raise RuntimeError('Please install 64-bit Python 3.10+ (3.12 recommended).')
    root = Path(__file__).resolve().parent
    os.chdir(root)
    python = root / '.venv' / 'Scripts' / 'python.exe'
    if not python.exists():
        print('Creating local Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(root / '.venv')
    requirements = root / 'requirements.txt'
    stamp = root / '.venv' / 'installed-requirements.txt'
    wanted = requirements.read_bytes()
    probe = subprocess.run([str(python), '-c', 'import requests, numpy, PIL, cv2, uiautomation, tkinter'], capture_output=True)
    if probe.returncode or not stamp.exists() or stamp.read_bytes() != wanted:
        print('Installing dependencies (Internet required on first launch)...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
        stamp.write_bytes(wanted)
    return subprocess.call([str(python), str(root / 'signup_helper.py')])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('Startup failed:', exc)
        sys.exit(1)
