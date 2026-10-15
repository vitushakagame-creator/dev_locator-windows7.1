#!/usr/bin/env python3
"""Создаёт Data/<ник>/ и копирует туда сайт и локальные инструменты DevSpace."""
from __future__ import annotations
import json, platform, re, shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'Data'

def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or '').strip()).rstrip('. ')
    return value[:80] or 'user'

def main():
    nickname = input('Введи свой ник DevSpace: ').strip()
    if len(nickname) < 2:
        print('Ник должен содержать минимум 2 символа.'); return 1
    user_dir = DATA / safe_name(nickname)
    (user_dir / 'site').mkdir(parents=True, exist_ok=True)
    (user_dir / 'tools').mkdir(parents=True, exist_ok=True)
    (user_dir / 'projects').mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ('index.html', 'devspace_data_helper.js', 'devspace_import.py', 'README_DevSpace_GitHub_Python.md'):
        source = ROOT / name
        if source.is_file():
            target = user_dir / ('site' if name == 'index.html' else 'tools') / name
            shutil.copy2(source, target); copied.append(str(target.relative_to(ROOT)))
    manifest = {
        'type': 'devspace-local-project', 'nickname': nickname,
        'createdAt': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'computerName': platform.node() or 'unknown-computer',
        'platform': platform.platform(), 'siteIsGitHubPagesStatic': True,
        'note': 'Эта папка хранит локальные копии и резервные данные. Сама по себе она не синхронизирует пользователей между устройствами.'
    }
    (user_dir / 'project.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (user_dir / 'README.txt').write_text(
        f'DevSpace — локальная папка пользователя: {nickname}\n\n'
        'site/index.html — копия сайта.\n'
        'tools/ — скрипты для работы с резервными копиями.\n'
        'projects/ — проекты, извлечённые из account JSON.\n'
        'Запусти devspace_import.py из корня комплекта, чтобы импортировать резервную копию.\n', encoding='utf-8')
    print(f'Готово: {user_dir}')
    print('Созданы папки site, tools и projects; имя компьютера записано в project.json.')
    print('Для сохранения проектов из JSON запусти devspace_import.py.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
