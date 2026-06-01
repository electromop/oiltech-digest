# Отчёт по парсингу источников — 2026-06-01

Парсинг `parse --max-age-days 7` (свежие статьи ≤7 дней). Проба статуса — независимый
параллельный GET на основной URL источника (rss_url для rss, listing_url/url для request),
заголовки как у приложения, timeout 12s, **без** SSL-fallback.

## Сводка
- Источников rss+request: **105** (rss 23, request 82). Telegram (15) этой командой не парсятся.
- Итог парсинга: **423 свежих статьи** с **60 источников**.
- Статусы пробы: OK 79 · BLOCKED 8 · SSL_ERR 8 · TIMEOUT 6 · CONN_ERR 3 · HTTP 412 — 1.
- Парсинг застрял на 6 TIMEOUT-источниках (request), остановлен досрочно (данных хватало).

## ВАЖНО про SSL_ERR
Проба делает «голый» запрос без SSL-fallback. 5 из 8 SSL_ERR-источников **реально отдали статьи**
через `verify=False`-fallback приложения: Hydrocarbon Processing (6), Neftegaz.ru (6), SOCAR (6),
Минобрнауки РФ (6), Сургутнефтегаз (5). То есть SSL_ERR ≠ заблокирован.

## Дали статьи (60 источников, 423 статьи)
**RSS:** EnergyLand 70, Rigzone 20, TechCrunch 20, Интерфакс ТЭК 19, LNG Industry 17, МФТИ 17,
McKinsey Energy Insights 13, MIT Technology Review 10, Oilfield Technology 7, VentureBeat 7,
Минэнерго РФ 7, Oil & Gas Journal 3, Норникель 3, Saipem 2, Automation World 1, Kimmeridge 1, ДРТ 1.
**Request (200):** ADNOC, Aker Solutions, BP, Deloitte, EIA, Halliburton, Hart Energy, Petrobras,
TechnipFMC, Upstream Online, World Oil, ЕЭК, Новатэк, Роснефть, РАН, Сколково Energy, Сколтех, ТеДо,
Томский политех, ЦДУ ТЭК, ЦСР — по 6; Лукойл 5, СПбГорный 5; IEA 4, Rystad 4, TotalEnergies 4, Сибур 4;
CNOOC 3; Eni 2, Equinor 2, JPT 2, SPE 2, КАМАЗ 2, КазМунайГаз 2; NOV 1, Б1 1, Яков и Партнёры 1.
**Request (SSL-fallback, дали статьи):** Hydrocarbon Processing 6, Neftegaz.ru 6, SOCAR 6, Минобрнауки 6, Сургутнефтегаз 5.

## Проблемные (0 статей) — для будущей задачи
**BLOCKED (WAF, 403/401):** BCG Energy, Baker Hughes, Energy Voice, IHS Markit / S&P Global,
S&P Global Commodity Insights, Weatherford, РБК Энергетика (401), СберТех (401).
**TIMEOUT (висли, подвесили прогон):** Saudi Aramco, Белоруснефть, Газпром нефть, Минпромторг РФ,
РГУ нефти и газа им. Губкина, Татнефть.
**CONN_ERR (DNS/соединение):** eLIBRARY, Росстандарт, Уфимский гос. нефтяной технический ун-т.
**HTTP 412:** CNPC.
**SSL_ERR без статей:** Pipeline & Gas Journal, Petroleum Economist (5*), Tatweer Petroleum (rss).
**OK (200), но 0 новых статей за неделю:** Bloomberg Energy, IoT World Today, Kuwait Oil Company,
OPEC, OilCapital, QatarEnergy, RusEnergy, SLB, Shell, Spears & Associates, Subsea7, Wood Mackenzie,
АЦ ТЭК, МГУ, Нефтегазовая вертикаль, Росатом, Ростех, Сбер, Узбекнефтегаз, Центр энергетики МШУ;
RSS: KazPetroDrilling, Mubadala Energy, Offshore Magazine, Petronas, СПбГУ.

## Связь с серверной проблемой (Timeweb)
Даже с локального (домашнего) IP ~26 источников проблемные (блок/таймаут/conn). На дата-центровом
IP Timeweb блоков будет больше — подтверждает, что причина в репутации IP, а не в коде.
