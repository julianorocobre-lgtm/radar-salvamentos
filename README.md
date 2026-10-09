# Radar de Salvamentos · Carlider

Revisa cada 15 minutos dónde se publican vehículos de salvamento en Colombia y avisa por Telegram cuando aparece uno nuevo dentro del rango de precio configurado.

**Fuentes**
- Superbid Colombia (AXA Colpatria, Zurich, SBS, Sura, Bolívar, HDI, Previsora)
- Subastas y Comercio (Sura, Allianz)
- Facebook Marketplace, vía Apify (una búsqueda cada 3 horas, con tope de USD 4,50 al mes)

**Archivos**
- `monitor_salvamentos.py` — el monitor.
- `config.json` — rango de precios y búsquedas de Facebook. Se cambia desde el tablero.
- `estado.json` — lo que ya se vio, para no avisar dos veces.
- `ultima_corrida.json` — resultado de la última revisión; de aquí se alimenta el tablero.
- `.github/workflows/radar.yml` — la programación cada 15 minutos.

Las claves (Apify y Telegram) están guardadas como *secrets* del repositorio, nunca en estos archivos.
