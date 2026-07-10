# fuel-map

Карта наличия топлива на АЗС (данные [toplivo.tbank.ru](https://toplivo.tbank.ru),
дополнительно время оплат с [sberazs.ru](https://sberazs.ru)) с маршрутизацией по реальным дорогам.

![status](https://img.shields.io/badge/stack-Leaflet%20%2B%20Python%20stdlib-blue)

## Возможности

- АЗС на карте (Leaflet + OpenStreetMap), цвет маркера — статус наличия топлива;
  подгрузка станций по видимой области карты
- Попап: наличие по видам топлива (АИ-92/95), время последней оплаты
  (Т-Банк и, если есть, Сбер), уверенность оценки
- Бейдж у маркера с относительным временем последней оплаты (зелёный < 3 ч, серый > 24 ч);
  второй строкой — время оплаты по данным Сбера (станции сопоставляются по координатам, до 150 м)
- Фильтры: по виду топлива и по статусу (есть / возможно / нет / нет данных)
- Маршрут по АЗС (TSP):
  - по всем видимым станциям, или по выбранным вручную
  - выбор точек: ctrl+клик — добавить сразу; клик → попап → «Старт» / «Точка» / «Финиш»
  - матрица времени в пути и геометрия — OSRM (реальные дороги), решатель:
    nearest-neighbour со всех стартов + 2-opt, с опциональной фиксацией старта/финиша
  - fallback на маршрут по прямой, если OSRM недоступен
  - ссылка «открыть в Я.Картах»

## Запуск

```
python server.py
# → http://localhost:8000
```

`server.py` — статический сервер + прокси `/api/*` → toplivo.tbank.ru,
`/sber/*` → sberazs.ru и `/osrm/*` → router.project-osrm.org
(у внешних API нет CORS-заголовков).

## Компактный exe (Rust, ~220 КБ)

```
build-rust.bat
# → target\release\fuel-host.exe

fuel-host.exe        # порт 8000
fuel-host.exe 8123   # свой порт
```

Тот же сервер (статика + все три прокси), но без интерпретатора внутри:
`index.html` вшит на этапе компиляции, TLS берётся из Windows (SChannel),
поэтому бинарник ~210 КБ вместо ~9 МБ у PyInstaller. Лежащий рядом с exe
`index.html` тоже имеет приоритет над встроенным.

## Standalone exe (PyInstaller, ~9 МБ)

```
build.bat
```

(ставит PyInstaller при необходимости, останавливает запущенный exe и собирает заново;
вручную: `python -m PyInstaller --onefile --name fuel-map-server --add-data "index.html;." server.py`)

`dist/fuel-map-server.exe` полностью автономен (`index.html` зашит внутрь) —
достаточно скопировать и запустить. Если рядом с exe положить свой `index.html`,
сервер отдаст его вместо встроенного.

## Релизы и Docker

Каждый push в `main` автоматически повышает patch-версию (тег `vX.Y.Z`),
публикует GitHub Release с `fuel-map-vX.Y.Z-win64.zip` (exe + README) и
docker-образ:

```
docker run -d -p 8000:8000 ghcr.io/mrkhrd/fuel-map:latest
docker run -d -p 8123:8123 ghcr.io/mrkhrd/fuel-map:latest 8123   # свой порт
```

Версия зашивается в бинарник и печатается при старте; сервер пишет в консоль
все запросы клиента и походы к внешним API с таймингами.

## Автозапуск на Windows

```powershell
netsh advfirewall firewall add rule name="fuel-map" dir=in action=allow protocol=TCP localport=8000
schtasks /Create /TN "fuel-map" /SC ONSTART /RU SYSTEM /TR "C:\path\to\fuel-map-server.exe"
schtasks /Run /TN "fuel-map"
```

## Замечания

- Публичный OSRM demo-сервер rate-limited — для личного использования достаточно,
  при недоступности маршрут строится по прямой.
- Сервер без аутентификации и HTTPS — рассчитан на локальную сеть.
