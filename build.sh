#!/usr/bin/env bash
# Сборка программы в один исполняемый файл для Ubuntu: dist/laserburn
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements-dev.txt
rm -rf build dist ./*.spec
.venv/bin/python -m PyInstaller --noconfirm --onefile --windowed --name laserburn \
    --add-data "laserburn/resources:laserburn/resources" \
    main.py
echo "Готово: dist/laserburn ($(du -h dist/laserburn | cut -f1))"
