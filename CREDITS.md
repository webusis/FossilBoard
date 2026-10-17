# CREDITS

## Тема оформления: Yaru Dark

Палитра и имена переменных взяты из исходников темы Ubuntu Yaru,
репозиторий <https://github.com/ubuntu/yaru> (ветка `master`).

Что и откуда:

| Что | Путь в репозитории yaru |
|---|---|
| Карта имён → CSS-переменные (`-yaru-<имя>`) | `common/yaru-colors-defs.scss` |
| Значения цветов, ветки light/dark | `gtk/src/default/gtk-4.0/_colors.scss` |
| Базовая палитра (aubergine, orange, inkstone, jet…) | `gtk/src/default/gtk-4.0/_palette.scss`, `gnome-shell/src/gnome-shell-sass/_yaru-palette.scss` |

Значения вычислены из SCSS-функций `lighten()`, `darken()`, `mix()` для
`$variant == 'dark'` и вшиты в `fossilboard/web/style.css` как переменные
`--yaru-*`. В рантайме ничего не скачивается: ни CDN, ни шрифтов, ни тем.

Лицензия yaru — GPL-3.0 / CC BY-SA 3.0 (см. `LICENSE` в том репозитории).
Это не влияет на лицензию FossilBoard (MIT), потому что здесь взяты только
**числовые значения цветов** — факты, не охраняемый авторским правом элемент:
принцип «названия и числа — факты, выразительные элементы — авторские».

## Иконки

Все иконки — собственные inline-SVG в духе Yaru (минимализм, обводка,
`currentColor`, 16–20 px). Ничего не скопировано, поэтому атрибуция
CC BY-SA не требуется.

## Скриншоты интерфейса

Сняты headless Chromium из локального кэша Playwright
(`~/.cache/ms-playwright`), запуск с `--no-sandbox`. Это инструмент
разработки, не зависимость проекта.