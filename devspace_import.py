#!/usr/bin/env python3
"""Импортирует JSON-резервную копию DevSpace в Data/<ник>/ на этом компьютере."""
from __future__ import annotations
import base64, json, mimetypes, os, platform, re, shutil, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_to_bytes

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'Data'

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

def safe_name(value: str) -> str:
    value = str(value or 'user').strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value)
    value = value.rstrip('. ')
    return (value[:80] or 'user')

def decode_data_uri(data: str):
    if not isinstance(data, str) or not data.startswith('data:') or ',' not in data:
        return None, None
    header, payload = data.split(',', 1)
    mime = header[5:].split(';', 1)[0] or 'application/octet-stream'
    try:
        raw = base64.b64decode(payload) if ';base64' in header else unquote_to_bytes(payload)
    except Exception:
        return mime, None
    return mime, raw

def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def import_backup(source: Path) -> Path:
    backup = json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(backup, dict) or backup.get('type') != 'devspace-account' or not backup.get('name'):
        raise ValueError('Выбранный файл не похож на резервную копию DevSpace.')
    nickname = str(backup['name'])
    user_dir = DATA_DIR / safe_name(nickname)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    import_dir = user_dir / 'imports' / stamp
    projects_dir = user_dir / 'projects'
    import_dir.mkdir(parents=True, exist_ok=True)
    projects_dir.mkdir(parents=True, exist_ok=True)

    # Keep the original backup intact, with the import machine and timestamp recorded separately.
    shutil.copy2(source, import_dir / 'account-original.json')
    enriched = dict(backup)
    enriched['importedAt'] = now_iso()
    enriched['importedOnComputer'] = platform.node() or 'unknown-computer'
    enriched['importedOnPlatform'] = platform.platform()
    write_json(import_dir / 'account-import-info.json', {
        'nickname': nickname,
        'importedAt': enriched['importedAt'],
        'computerName': enriched['importedOnComputer'],
        'platform': enriched['importedOnPlatform'],
        'sourceFile': source.name,
        'projectsInBackup': len(backup.get('projects') or []),
    })
    write_json(user_dir / 'account.json', enriched)

    project_index = []
    for idx, project in enumerate(backup.get('projects') or [], start=1):
        if not isinstance(project, dict):
            continue
        title = str(project.get('title') or f'Project {idx}')
        folder = projects_dir / f'{idx:02d}-{safe_name(title)}'
        folder.mkdir(parents=True, exist_ok=True)
        files_index = []
        for file_idx, file_obj in enumerate(project.get('files') or [], start=1):
            if not isinstance(file_obj, dict):
                continue
            original_name = safe_name(Path(str(file_obj.get('name') or f'file-{file_idx}')).name)
            data = file_obj.get('data')
            mime, raw = decode_data_uri(data) if data else (None, None)
            status = 'metadata-only'
            if raw is not None:
                target = folder / original_name
                # Avoid silently overwriting duplicate names within one project.
                if target.exists():
                    target = folder / f'{file_idx:02d}-{original_name}'
                target.write_bytes(raw)
                status = 'saved'
            files_index.append({
                'name': original_name,
                'size': file_obj.get('size'),
                'mime': file_obj.get('mime') or mime,
                'status': status,
            })
        metadata = {k: v for k, v in project.items() if k != 'files'}
        metadata['filesExtracted'] = files_index
        write_json(folder / 'project.json', metadata)
        project_index.append({'title': title, 'folder': str(folder.relative_to(user_dir)), 'files': files_index})
    write_json(user_dir / 'projects-index.json', project_index)
    return user_dir

def main():
    try:
        if len(sys.argv) > 1:
            source = Path(sys.argv[1]).expanduser().resolve()
        else:
            # Use a file picker on Windows; allow a typed path if tkinter is unavailable.
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
                selected = filedialog.askopenfilename(title='Выбери JSON-файл аккаунта DevSpace', filetypes=[('DevSpace JSON', '*.json'), ('Все файлы', '*.*')])
                root.destroy()
                if not selected:
                    print('Отменено: файл не выбран.'); return 1
                source = Path(selected)
            except Exception:
                source = Path(input('Путь к JSON-файлу аккаунта DevSpace: ').strip().strip('"')).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f'Файл не найден: {source}')
        out = import_backup(source)
        print(f'Готово! Данные сохранены в:\n{out}')
        print(f'Имя компьютера: {platform.node() or "unknown-computer"}')
        print('Проверь папку projects: файлы с содержимым извлечены, если они были включены в JSON.')
        return 0
    except Exception as exc:
        print(f'Не удалось импортировать резервную копию: {exc}')
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
