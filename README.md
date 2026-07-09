# fuel-map

Карта наличия топлива на АЗС (данные [toplivo.tbank.ru](https://toplivo.tbank.ru)) с маршрутизацией по реальным дорогам.

![status](https://img.shields.io/badge/stack-Leaflet%20%2B%20Python%20stdlib-blue)

## Возможности

- АЗС на карте (Leaflet + OpenStreetMap), цвет маркера — статус наличия топлива;
  подгрузка станций по видимой области карты
- Попап: наличие по видам топлива (АИ-92/95), время последней оплаты, уверенность оценки
- Бейдж у маркера с относительным временем последней оплаты (зелёный < 3 ч, серый > 24 ч)
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

`server.py` — статический сервер + прокси `/api/*` → toplivo.tbank.ru и
`/osrm/*` → router.project-osrm.org (у обоих API нет CORS-заголовков).

## Standalone exe (без Python)

```
pip install pyinstaller
python -m PyInstaller --onefile --name fuel-map-server server.py
```

Скопировать `dist/fuel-map-server.exe` + `index.html` в одну папку и запустить.

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
