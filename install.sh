#!/usr/bin/env bash
# Установка LaserBurn Analogue в систему: команда laserburn и ярлык в меню приложений.
#   ./install.sh               — для текущего пользователя (~/.local), без sudo
#   ./install.sh --system      — для всех пользователей (/usr/local), через sudo
#   ./install.sh --uninstall   — удалить (вместе с --system, если ставили для всех)
set -euo pipefail
cd "$(dirname "$0")"

PREFIX="$HOME/.local"
SUDO=""
ACTION=install
for arg in "$@"; do
    case "$arg" in
        --system) PREFIX=/usr/local; SUDO=sudo ;;
        --uninstall) ACTION=uninstall ;;
        *) echo "Неизвестный параметр: $arg"; exit 1 ;;
    esac
done

BIN="$PREFIX/bin/laserburn"
ICON="$PREFIX/share/icons/hicolor/256x256/apps/laserburn.png"
DESKTOP="$PREFIX/share/applications/laserburn.desktop"

refresh_menu() {
    command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$PREFIX/share/applications" || true
    command -v gtk-update-icon-cache >/dev/null && $SUDO gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" || true
}

if [ "$ACTION" = uninstall ]; then
    $SUDO rm -f "$BIN" "$ICON" "$DESKTOP"
    refresh_menu
    echo "Удалено из $PREFIX"
    exit 0
fi

if [ ! -x dist/laserburn ]; then
    echo "Исполняемый файл не собран, запускаю сборку…"
    ./build.sh
fi

$SUDO install -Dm755 dist/laserburn "$BIN"
$SUDO install -Dm644 laserburn/resources/icon.png "$ICON"
TMP_DESKTOP=$(mktemp)
cat > "$TMP_DESKTOP" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=LaserBurn Analogue
GenericName=Управление лазерным гравёром
Comment=Подготовка макетов и отправка заданий на лазерный станок с GRBL
Exec=$BIN %f
Icon=laserburn
Terminal=false
Categories=Graphics;Engineering;
Keywords=laser;grbl;cnc;gcode;лазер;гравёр;
StartupWMClass=laserburn
DESKTOP_EOF
$SUDO install -Dm644 "$TMP_DESKTOP" "$DESKTOP"
rm -f "$TMP_DESKTOP"
refresh_menu

echo "Установлено: $BIN"
echo "Ярлык «LaserBurn Analogue» появился в меню приложений."
case ":$PATH:" in
    *":$PREFIX/bin:"*) echo "Запуск из терминала: laserburn" ;;
    *) echo "Каталог $PREFIX/bin пока не в PATH — перелогиньтесь, после этого заработает команда laserburn" ;;
esac
if ! id -nG "$USER" | grep -qw dialout; then
    echo
    echo "ВНИМАНИЕ: для доступа к USB-порту станка выполните и перелогиньтесь:"
    echo "  sudo usermod -a -G dialout \$USER"
fi
