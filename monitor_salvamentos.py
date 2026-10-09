#!/usr/bin/env python3
"""Monitor de vehículos de salvamento en Colombia.

Revisa las fuentes, compara con lo ya visto (estado.json) y:
  - imprime en stdout un JSON con {"nuevos": [...], "activos": [...], "resumen": {...}}
  - lanza una notificación de macOS por cada tanda de lotes nuevos
  - (opcional) envía un mensaje a Telegram si existen TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID

Uso:  python3 monitor_salvamentos.py [--sin-notificar] [--reset] [--importar avisos.json]

config.json (junto al script) define el rango de precios y las búsquedas:
  {"precio_min": 0, "precio_max": 60000000,
   "fb_busquedas": ["carro chocado", "salvamento"], "fb_ciudad": "bogota"}
Solo los lotes dentro del rango disparan alarma; todos quedan registrados.
--importar agrega avisos ya normalizados de otras fuentes (p. ej. Facebook Marketplace vía Apify).
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime

AQUI = os.path.dirname(os.path.abspath(__file__))
ESTADO = os.path.join(AQUI, "estado.json")
CONFIG = os.path.join(AQUI, "config.json")
CONFIG_BASE = {"precio_min": 0, "precio_max": 0, "fb_busquedas": [], "fb_ciudad": "", "fb_cada_min": 180,
               "fb_dias_visibles": 14, "fb_presupuesto_usd_mes": 4.5}
# Tarifas del actor en el plan gratuito de Apify (USD): arranque por corrida y por aviso entregado
FB_COSTO_ARRANQUE, FB_COSTO_AVISO = 0.005, 0.0015
NO_VEHICULO = re.compile(r"plotter|betamax|herramienta|rachet|ratchet|copas|soporte para bici|sacatocos|"
                         r"impresora|celular|televisor|lavadora|nevera|computador|port[aá]til", re.I)
FB_ACTOR = "memo23~facebook-marketplace-scraper-ppe"


def cargar_env():
    """Lee claves.txt o .env (APIFY_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) sin sobrescribir el entorno."""
    for nombre in ("claves.txt", ".env"):
        ruta = os.path.join(AQUI, nombre)
        if os.path.exists(ruta):
            break
    else:
        return
    for linea in open(ruta):
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"}

ASEGURADORAS = ["SURA", "SURAMERICANA", "BOLIVAR", "BOLÍVAR", "AXA", "COLPATRIA", "ZURICH", "SBS", "HDI",
                "LIBERTY", "PREVISORA", "MAPFRE", "ALLIANZ", "EQUIDAD", "ESTADO", "SOLIDARIA", "MUNDIAL"]
PALABRAS_SALV = re.compile(r"salvamento|siniestr|p[ée]rdida total|\bsura\b|hurto recuperado|amparo:", re.I)


def get(url, timeout=40):
    # curl en vez de urllib: el Python de macOS trae un SSL viejo que algunos sitios rechazan
    r = subprocess.run(["curl", "-sgL", "--fail", "-m", str(timeout), "-A", UA["User-Agent"], url],
                       capture_output=True, check=True)
    return r.stdout.decode("utf-8", "ignore")


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s


# ---------------------------------------------------------------- Superbid Colombia
def superbid():
    url = ("https://offer-query.superbid.net/offers/?locale=es_CO&timeZoneId=America/Bogota"
           "&portalId=[17]&searchType=opened&pageNumber=1&pageSize=500")
    data = json.loads(get(url))
    out = []
    for o in data.get("offers", []):
        p = o.get("product") or {}
        auc = o.get("auction") or {}
        vendedor = (o.get("seller") or {}).get("name") or ""
        subcat = ((p.get("subCategory") or {}).get("description") or "")
        texto = f"{subcat} {auc.get('desc','')} {vendedor}".upper()
        es_salv = ("SINIESTR" in texto or "SALVAMENTO" in texto
                   or any(a in vendedor.upper() for a in ASEGURADORAS))
        if not es_salv:
            continue
        loc = p.get("location") or {}
        titulo = (p.get("shortDesc") or "").strip()
        out.append({
            "id": f"superbid-{o['id']}",
            "fuente": "Superbid",
            "titulo": titulo,
            "vendedor": vendedor.title(),
            "subasta": auc.get("desc", ""),
            "ciudad": loc.get("city") or "",
            "precio": o.get("price") or 0,
            "cierre": o.get("endDate") or "",
            "publicado": o.get("publishedAt") or o.get("createAt") or "",
            "foto": p.get("thumbnailUrl") or "",
            "url": f"https://www.superbid.com.co/oferta/{slug(titulo)}-{o['id']}",
        })
    return out


# ---------------------------------------------------------------- Subastas y Comercio
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
CARD = re.compile(
    r'<img src="(?P<foto>[^"]+)"[^>]*alt="[^"]*">\s*<div class="card-body-custom">\s*'
    r'<h5 class="card-title[^"]*">(?P<titulo>.*?)</h5>\s*<h5 class="custom-card-subtittle">(?P<codigo>.*?)</h5>'
    r'(?P<resto>.*?)<a href="(?P<url>https://subastasycomercio\.com/detalle-lote-subastas-web/[^"]+)"', re.S)


def syc_detalle_es_salvamento(url, cache):
    if url in cache:
        return cache[url]
    try:
        t = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", get(url, 25), flags=re.S)
        m = re.search(r"Detalles del Lote(.{0,2500})", t, re.S)
        cuerpo = m.group(1) if m else t
        es = bool(PALABRAS_SALV.search(cuerpo)) and "recuperación bancaria" not in cuerpo.lower()
    except Exception:
        es = False
    cache[url] = es
    return es


def subastas_y_comercio(cache):
    out, vistos = [], set()
    for cat in ["salvamentos", "vehiculos"]:
        h = get(f"https://subastasycomercio.com/categorias-subastas-web/{cat}.html")
        for m in CARD.finditer(h):
            url = m.group("url")
            if url in vistos:
                continue
            vistos.add(url)
            resto = m.group("resto")
            cierre = re.search(r'data-close-date="([^"]+)"', resto)
            if not cierre:
                continue  # sin fecha de cierre = lote ya no activo
            precio = re.search(r"\$([\d,\.]+)\s*COP", resto)
            ciudad = re.search(r'bi-geo-alt-fill[^>]*></i>\s*([^<]+)', resto)
            es = "/salvamentos/" in url or syc_detalle_es_salvamento(url, cache)
            if not es:
                continue
            lid = re.search(r"/(\d+)-[^/]+\.html$", url)
            out.append({
                "id": f"syc-{lid.group(1) if lid else slug(url)}",
                "fuente": "Subastas y Comercio",
                "titulo": html.unescape(m.group("titulo")).strip(),
                "vendedor": "",
                "subasta": html.unescape(m.group("codigo")).strip(),
                "ciudad": (ciudad.group(1).strip() if ciudad else ""),
                "precio": int(re.sub(r"[^\d]", "", precio.group(1))) if precio else 0,
                "cierre": cierre.group(1),
                "publicado": "",
                "foto": m.group("foto"),
                "url": url,
            })
    return out


# ---------------------------------------------------------------- Facebook Marketplace (vía Apify)
def precio_fb(valor):
    """En Marketplace muchos publican '29' por 29 millones, o '1111' / '0' como relleno."""
    if isinstance(valor, dict):
        valor = valor.get("amount")
    try:
        n = int(float(valor or 0))
    except ValueError:
        return 0
    if 0 < n <= 150:
        return n * 1_000_000  # "29" = 29 millones; más de 150 millones así escrito no es creíble
    if n < 1000:
        return 0
    if n < 1_000_000:
        return 0  # precio de relleno: sin dato
    return n


# Marketplace necesita la ciudad como aparece en su dirección web; se aceptan departamentos y tildes
FB_UBICACION = {"antioquia": "medellin", "medellín": "medellin", "cundinamarca": "bogota", "bogotá": "bogota",
                "valle": "cali", "valle del cauca": "cali", "atlantico": "barranquilla", "atlántico": "barranquilla",
                "santander": "bucaramanga", "bolivar": "cartagena", "bolívar": "cartagena",
                "risaralda": "pereira", "caldas": "manizales", "tolima": "ibague", "ibagué": "ibague",
                "meta": "villavicencio", "boyaca": "tunja", "boyacá": "tunja", "huila": "neiva",
                "norte de santander": "cucuta", "cúcuta": "cucuta", "quindio": "armenia", "quindío": "armenia"}


def ubicacion_fb(texto):
    t = (texto or "bogota").strip().lower()
    return FB_UBICACION.get(t, t.replace(" ", ""))


def facebook(cfg, estado):
    tok = os.environ.get("APIFY_TOKEN")
    if not tok or not cfg.get("fb_busquedas"):
        return []
    ahora = datetime.now()
    ultima = estado.get("fb_ultima")
    cache = estado.setdefault("fb_cache", {})
    debe_correr = not ultima or (ahora - datetime.fromisoformat(ultima)).total_seconds() >= cfg["fb_cada_min"] * 60
    # si cambiaron las búsquedas, la ciudad o el rango desde el tablero, se busca de una vez
    firma = json.dumps([cfg.get("fb_busquedas"), cfg.get("fb_ciudad"), cfg.get("precio_max")], ensure_ascii=False)
    if estado.get("fb_firma") != firma:
        debe_correr = True
        estado["fb_turno"] = 0
    estado["fb_firma"] = firma
    mes = ahora.strftime("%Y-%m")
    gasto = estado.setdefault("fb_gasto", {})
    if gasto.get("mes") != mes:
        gasto.clear(); gasto.update(mes=mes, usd=0.0, corridas=0, avisos=0)
    if debe_correr and gasto["usd"] + FB_COSTO_ARRANQUE + 20 * FB_COSTO_AVISO > cfg["fb_presupuesto_usd_mes"]:
        debe_correr = False  # presupuesto del mes agotado: Facebook se pausa hasta el próximo mes
        estado["fb_pausado"] = f"Presupuesto de USD {cfg['fb_presupuesto_usd_mes']} del mes agotado"
    if debe_correr:
        estado.pop("fb_pausado", None)
        estado["fb_ultima"] = ahora.isoformat(timespec="seconds")
        ventana_h = max(3, cfg["fb_cada_min"] * 3 // 60)
        # plan gratuito: una búsqueda por corrida, rotando entre las palabras configuradas
        turno = estado.get("fb_turno", 0) % len(cfg["fb_busquedas"])
        estado["fb_turno"] = turno + 1
        for q in [cfg["fb_busquedas"][turno]]:
            entrada = {"searchQuery": q, "marketplaceLocation": ubicacion_fb(cfg.get("fb_ciudad")),
                       "categories": ["vehicles"], "sortBy": "creation_time_descend", "exact": True,
                       "maxItems": 20, "flattenOutput": True,
                       "maxListingAgeHours": 0 if not ultima else ventana_h}
            if cfg.get("precio_max"):
                entrada["maxPrice"] = cfg["precio_max"]
            r = subprocess.run(
                ["curl", "-sS", "--fail", "-m", "240", "-X", "POST",
                 "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json",
                 "-d", json.dumps(entrada),
                 f"https://api.apify.com/v2/acts/{FB_ACTOR}/run-sync-get-dataset-items?maxTotalChargeUsd=0.15&timeout=200"],
                capture_output=True, check=True)
            crudos = json.loads(r.stdout or b"[]")
            gasto["corridas"] += 1
            gasto["avisos"] += len(crudos)
            gasto["usd"] = round(gasto["corridas"] * FB_COSTO_ARRANQUE + gasto["avisos"] * FB_COSTO_AVISO, 4)
            for a in crudos:
                if not a.get("id") or a.get("isSold") or a.get("isPending"):
                    continue
                if NO_VEHICULO.search(a.get("listingTitle") or ""):
                    continue
                cache[f"fb-{a['id']}"] = {
                    "id": f"fb-{a['id']}",
                    "fuente": "Facebook Marketplace",
                    "titulo": (a.get("listingTitle") or "").replace("\n", " ").strip(),
                    "vendedor": "",
                    "subasta": f"Búsqueda: {q}",
                    "ciudad": a.get("locationText") or "",
                    "precio": precio_fb(a.get("listingPrice") or a.get("listingPrice.amount")),
                    "cierre": "",
                    "publicado": (a.get("timestamp") or "")[:19].replace("T", " "),
                    "foto": ((a.get("primaryListingPhoto") or {}).get("photo_image_url")
                             or a.get("primaryListingPhoto.photo_image_url") or ""),
                    "url": a.get("itemUrl") or f"https://www.facebook.com/marketplace/item/{a['id']}",
                }
    # se muestran los avisos de los últimos N días; los más viejos salen del tablero
    limite = ahora.timestamp() - cfg["fb_dias_visibles"] * 86400
    for k in [k for k, v in cache.items() if v["publicado"] and datetime.fromisoformat(v["publicado"]).timestamp() < limite]:
        del cache[k]
    return list(cache.values())


# ---------------------------------------------------------------- alertas
def notificar_mac(titulo, texto):
    if sys.platform != "darwin":
        return  # en GitHub (Linux) no hay notificaciones de Mac; queda Telegram
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(texto)}" with title "{esc(titulo)}" sound name "Glass"'],
                   check=False)


def notificar_telegram_lotes(lotes):
    """Una alarma por lote con su foto (hasta 10); si hay más, el resto va en un mensaje de lista."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    for destino in [c.strip() for c in chat.split(",") if c.strip()]:
        for l in lotes[:10]:
            texto = (f"🚗 {l['titulo']}\n💰 ${l['precio']:,}".replace(",", ".")
                     + f"\n📍 {l['ciudad'] or 'sin ciudad'} · {l['fuente']}"
                     + (f"\n⏰ Cierre {l['cierre']}" if l.get("cierre") else "")
                     + f"\n{l['url']}")
            base = ["curl", "-sS", "--fail", "-m", "30", "--data-urlencode", f"chat_id={destino}"]
            enviado = False
            if l.get("foto"):
                r = subprocess.run(base + ["--data-urlencode", f"photo={l['foto']}", "--data-urlencode",
                                           f"caption={texto[:1000]}",
                                           f"https://api.telegram.org/bot{tok}/sendPhoto"], capture_output=True)
                enviado = r.returncode == 0
            if not enviado:  # sin foto o Telegram no pudo descargarla
                subprocess.run(base + ["--data-urlencode", f"text={texto}", "-d", "disable_web_page_preview=true",
                                       f"https://api.telegram.org/bot{tok}/sendMessage"], capture_output=True)
        if len(lotes) > 10:
            resto = "\n".join(f"• {l['titulo']} — ${l['precio']:,} — {l['url']}" for l in lotes[10:25])
            subprocess.run(["curl", "-sS", "-m", "30", "--data-urlencode", f"chat_id={destino}",
                            "--data-urlencode", f"text=Y {len(lotes) - 10} lotes nuevos más:\n{resto}",
                            "-d", "disable_web_page_preview=true",
                            f"https://api.telegram.org/bot{tok}/sendMessage"], capture_output=True)


def notificar_telegram(texto):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    # TELEGRAM_CHAT_ID admite varios destinos separados por coma (tu chat, un grupo de socios, ...)
    for destino in [c.strip() for c in chat.split(",") if c.strip()]:
        try:
            subprocess.run(["curl", "-sS", "--fail", "-m", "20", f"https://api.telegram.org/bot{tok}/sendMessage",
                            "--data-urlencode", f"chat_id={destino}", "--data-urlencode", f"text={texto}",
                            "-d", "disable_web_page_preview=true"], capture_output=True, check=True)
        except Exception as e:
            print(f"telegram {destino}: {e}", file=sys.stderr)


def cargar_config():
    cfg = dict(CONFIG_BASE)
    if os.path.exists(CONFIG):
        cfg.update(json.load(open(CONFIG)))
    return cfg


def en_rango(lote, cfg):
    p = lote.get("precio") or 0
    if cfg.get("precio_min") and p and p < cfg["precio_min"]:
        return False
    if cfg.get("precio_max") and (not p or p > cfg["precio_max"]):
        return False
    return True


def importar(ruta):
    campos = ["id", "fuente", "titulo", "vendedor", "subasta", "ciudad", "precio", "cierre", "publicado", "foto", "url"]
    return [{k: a.get(k, "" if k != "precio" else 0) for k in campos} for a in json.load(open(ruta)) if a.get("id")]


def main():
    cargar_env()
    notificar = "--sin-notificar" not in sys.argv
    cfg = cargar_config()
    estado = {"vistos": {}, "cache_syc": {}}
    if os.path.exists(ESTADO) and "--reset" not in sys.argv:
        estado = json.load(open(ESTADO))

    lotes, errores = [], []
    fuentes = [("Superbid", superbid),
               ("Subastas y Comercio", lambda: subastas_y_comercio(estado["cache_syc"])),
               ("Facebook Marketplace", lambda: facebook(cfg, estado))]
    for nombre, fn in fuentes:
        try:
            lotes += fn()
        except Exception as e:
            errores.append(f"{nombre}: {e}")
    if "--importar" in sys.argv:
        ruta = sys.argv[sys.argv.index("--importar") + 1]
        try:
            lotes += importar(ruta)
        except Exception as e:
            errores.append(f"importar {ruta}: {e}")

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    primera_vez = not estado["vistos"]
    nuevos = [l for l in lotes if l["id"] not in estado["vistos"]]
    for l in lotes:
        l["en_rango"] = en_rango(l, cfg)
        l["detectado"] = estado["vistos"].get(l["id"], ahora)
        estado["vistos"][l["id"]] = l["detectado"]
    estado["ultima_revision"] = ahora
    json.dump(estado, open(ESTADO, "w"), ensure_ascii=False, indent=1)

    alertas = [l for l in nuevos if l["en_rango"]]
    if alertas and notificar and not primera_vez:
        nuevos_todos, nuevos = nuevos, alertas
        resumen = "; ".join(f"{l['titulo'][:40]} ({l['fuente']})" for l in nuevos[:3])
        extra = f" y {len(nuevos) - 3} más" if len(nuevos) > 3 else ""
        notificar_mac(f"🚗 {len(nuevos)} salvamento(s) nuevo(s)", resumen + extra)
        notificar_telegram_lotes(nuevos)
        nuevos = nuevos_todos

    salida = {"revision": ahora, "primera_vez": primera_vez, "errores": errores, "config": cfg,
              "resumen": {"activos": len(lotes), "nuevos": len(nuevos), "alertas": len(alertas)},
              "nuevos": nuevos, "activos": lotes}
    # la sincronización del tablero lee este archivo
    json.dump(salida, open(os.path.join(AQUI, "ultima_corrida.json"), "w"), ensure_ascii=False, indent=1)
    json.dump(salida, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
