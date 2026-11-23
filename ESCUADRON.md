# Versión Escuadrón

Fork de [LED-N/Fortnite-ChestCounter](https://github.com/LED-N/Fortnite-ChestCounter) (licencia MIT) que agrega el envío de partidas a un servidor propio, para llevar el conteo de un escuadrón entre varias PCs.

## Configuración

Crea `escuadron.json` junto a `ChestCounter.exe` (o en `%APPDATA%\ChestCounter`):

```json
{ "url": "https://tu-servidor", "clave": "la-clave-de-cada-jugador" }
```

## Qué se manda

El registro de cada partida que el programa ya guarda en `data.json` (`MatchRecord`): id de la partida, cuenta de Epic, cofres abiertos, cofres de los compañeros vistos en la repetición y tesoros. Por HTTPS, con la clave en la cabecera `X-Clave`. El código está en `src/ChestCounter/EscuadronSync.cs`.

## Servidor

Ver la carpeta `servidor/`.
