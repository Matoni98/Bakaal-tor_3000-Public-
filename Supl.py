import json
import os
import time
import smtplib
import logging
import schedule
import gc
import re

from logging.handlers import RotatingFileHandler
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import date, datetime, timedelta

from selenium.webdriver.chrome.options import Options as ChromeOptions

from collections import defaultdict
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from email.message import EmailMessage

# ============================================================
# ⚙️  KONFIGURACE
# ============================================================

# Nastav pracovní složku na složku scriptu
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)

# Vytvoř složku logs, pokud neexistuje
logs_dir = os.path.join(script_dir, "logs")
os.makedirs(logs_dir, exist_ok=True)

LOGIN_URL        = "https://zsamsbohuslavice.bakalari.cz/login"
SUPLOVANI_URL    = "https://zsamsbohuslavice.bakalari.cz/next/zmeny.aspx"

CACHE_SOUBOR          = "cache_suplovani.json"
KONTAKTY_SOUBOR       = "kontakty.json"
HISTORIE_SOUBOR       = "historie_suplovani.json"
MAX_CHYB              = 1
chyby_v_rade          = 0
HEADLESS = True  # Nastav na False pro zobrazení prohlížeče

load_dotenv()
ODESILATEL  = os.getenv("ODESILATEL")
HESLO       = os.getenv("HESLO")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", ODESILATEL)

# ============================================================
# 📋  LOGOVÁNÍ  –  rotující soubor (max 5 MB, 3 zálohy)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(os.path.join(logs_dir, "suplovani.log"), maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ============================================================
# 📜  HISTORIE ODESLANÝCH SUPLOVÁNÍ
# ============================================================


def nacti_historii() -> list:
    if not os.path.exists(HISTORIE_SOUBOR):
        return []
    try:
        with open(HISTORIE_SOUBOR, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def uloz_do_historie(zaznamy: list) -> None:
    """Přidá nové záznamy do historie s časovým razítkem odeslání."""
    historie = nacti_historii()
    odesilano_kdy = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in zaznamy:
        historie.append({
            "odesilano":  odesilano_kdy,
            "datum_supl": r[0],
            "ucitel":     r[1],
            "hodina":     r[2],
            "typ":        r[3],
            "predmet":    r[4],
            "trida":      r[5],
            "poznamka":   r[6] if len(r) > 6 else "",
        })
    with open(HISTORIE_SOUBOR, "w", encoding="utf-8") as f:
        json.dump(historie, f, ensure_ascii=False, indent=2)
    log.info(f"📜 Historie: přidáno {len(zaznamy)} záznamů.")

# ============================================================
# 🔢  POČÍTADLO CHYB
# ============================================================


def zaznamen_chybu(zprava: str) -> None:
    global chyby_v_rade
    chyby_v_rade += 1
    log.error(f"Chyba #{chyby_v_rade}: {zprava}")

    # Každou chybu pošli adminovi
    posli_email(
        ADMIN_EMAIL,
        zprava,
        predmet=f"⚠️ Chyba suplování #{chyby_v_rade}"
    )

    if chyby_v_rade >= MAX_CHYB:
        log.critical(f"🚨 Selhání {MAX_CHYB}x za sebou – informuji admina.")
        posli_email(
            ADMIN_EMAIL,
            f"Program selhal {MAX_CHYB}x za sebou!\n\nPoslední chyba:\n{zprava}\n\nZkontroluj server.",
            predmet="🚨 Kritické selhání – suplování"
        )
        chyby_v_rade = 0


def reset_chyb() -> None:
    global chyby_v_rade
    if chyby_v_rade > 0:
        log.info(f"✅ Obnoveno po {chyby_v_rade} chybách.")
    chyby_v_rade = 0


# ============================================================
# 📬  E-MAIL
# ============================================================

def posli_email(prijemce: str, zprava_text: str,
                predmet: str = "Informace o suplování",
                html: str = None) -> bool:
    for pokus in range(3):  # Zkusit 3x
        zprava = EmailMessage()
        zprava["Subject"] = predmet
        zprava["From"]    = ODESILATEL
        zprava["To"]      = prijemce
        zprava.set_content(zprava_text)
        if html:
            zprava.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP_SSL("smtp.webzdarma.cz", 465) as smtp:
                smtp.login(ODESILATEL, HESLO)
                smtp.send_message(zprava)
            log.info(f"✅ E-mail odeslán → {prijemce}")
            return True
        except Exception as e:
            log.error(f"❌ Chyba odesílání → {prijemce} (pokus {pokus+1}/3): {e}")
            if pokus < 2:  # Pauza před dalším pokusem
                time.sleep(10)
    return False

# ============================================================
# 📬  E-MAILY – SUPLOVÁNÍ
# ============================================================

def datum_pro_razeni(datum_str: str) -> date:
    """'7. 1. 2026' nebo '7.1.2026' → date objekt pro řazení."""
    try:
        cisla = re.findall(r'\d+', datum_str)
        if len(cisla) == 3:
            return date(int(cisla[2]), int(cisla[1]), int(cisla[0]))
    except Exception:
        pass
    return date.min

def _html_suplovani(jmeno: str, seznam: list) -> str:
    """HTML email pro učitele se suplováním."""
    dnes = date.today().strftime("%d.%m.%Y")
    # Datumy které supluje (unikátní)
    datumy = sorted({z[0] for z in seznam}, key=lambda d: datum_pro_razeni(d))
    datumy_str = ", ".join(datumy)

    radky_html = ""
    for z in seznam:
        # z = [datum, jmeno, hodina, typ, predmet, trida, poznamka]
        poznamka = z[6] if z[6] else ""
        radky_html += (
            f'<tr>'
            f'<td>{z[0]}</td>'
            f'<td style="text-align:center;">{z[2]}.</td>'
            f'<td>{z[3]}</td>'
            f'<td>{z[4]}</td>'
            f'<td>{z[5]}</td>'
            f'<td style="color:#666;">{poznamka}</td>'
            f'</tr>\n'
        )

    txt = (
        f"Ahoj {jmeno},\n\n"
        f"máš {len(seznam)} suplování ({datumy_str}):\n\n"
        + "".join(
            f"  • {z[0]} | {z[2]}. hod. | {z[3]} | {z[4]} | {z[5]}"
            + (f" | {z[6]}" if z[6] else "") + "\n"
            for z in seznam
        )
        + "\nHezký den!\n"
    )

    html = f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8">
<style>
body{{font-family:Arial,sans-serif;font-size:14px;color:#222}}
h2{{font-size:17px;font-weight:500;margin:0 0 4px}}
.sub{{font-size:13px;color:#666;margin:0 0 16px}}
.stat{{display:inline-block;background:#e8f4fd;color:#1a4a80;border-radius:8px;
       padding:10px 20px;font-size:13px;margin-bottom:16px;font-weight:500}}
.stat b{{font-size:22px;display:block}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}}
th{{background:#2c3e50;color:#fff;padding:7px 9px;text-align:left;font-weight:500}}
td{{padding:6px 9px;border-bottom:0.5px solid #e0e0e0}}
tr:nth-child(even) td{{background:#f8f8f8}}
.footer{{font-size:11px;color:#aaa;margin-top:14px;padding-top:10px;
         border-top:0.5px solid #e0e0e0}}
</style></head><body>
<h2>Informace o suplování – {dnes}</h2>
<p class="sub">Ahoj {jmeno}, níže jsou tvá nadcházející suplování.</p>
<div class="stat">Počet hodin<b>{len(seznam)}</b></div>
<table>
  <thead><tr>
    <th>Datum</th><th>Hod.</th><th>Typ</th>
    <th>Předmět</th><th>Třída</th><th>Poznámka</th>
  </tr></thead>
  <tbody>
{radky_html}  </tbody>
</table>
<p class="footer">Automaticky vygenerováno systémem Bakalátor 3000 – suplování &amp; třídnice. Při jakýchkoli potížích kontaktujte Radka.</p>
</body></html>"""
    return txt, html


def rozesli_emaily_suplovani(zmenena_data: list, email_dict: dict) -> list:
    skupiny: dict = defaultdict(list)
    for r in zmenena_data:
        skupiny[r[1]].append(r)

    bez_emailu, odeslano = [], 0
    uspesne_zaznamy = []
    for jmeno, seznam in skupiny.items():
        # Křestní jméno pro oslovení (formát: Příjmení Jméno)
        casti = jmeno.split()
        krestni = casti[1] if len(casti) >= 2 else jmeno

        email = email_dict.get(jmeno)
        if not email:
            bez_emailu.append(jmeno)
            continue

        # Seřadíme datum → hodina
        seznam_s = sorted(seznam, key=lambda z: (datum_pro_razeni(z[0]), int(z[2]) if z[2].isdigit() else 0))

        # Datumy pro předmět emailu
        datumy = sorted({z[0] for z in seznam_s}, key=lambda d: datum_pro_razeni(d))
        datumy_str = ", ".join(datumy)
        predmet_email = f"Suplování ({len(seznam_s)} hod.) – {datumy_str}"

        txt, html = _html_suplovani(krestni, seznam_s)
        if posli_email(email, txt, predmet=predmet_email, html=html):
            odeslano += 1
            uspesne_zaznamy.extend(seznam)  # Přidat všechny záznamy pro tohoto učitele

    log.info(f"📊 Odesláno: {odeslano} e-mailů")
    if bez_emailu:
        log.warning(f"⚠️  Bez e-mailu: {', '.join(bez_emailu)}")
    return uspesne_zaznamy

# ============================================================
# 💾  CACHE  (porovnání změn – suplování)
# ============================================================

def nacti_cache() -> list:
    if not os.path.exists(CACHE_SOUBOR):
        return []
    try:
        with open(CACHE_SOUBOR, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def uloz_cache(data: list) -> None:
    with open(CACHE_SOUBOR, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def najdi_zmeny(stare: list, nove: list) -> list:
    stare_set = {tuple(r) for r in stare}
    return [r for r in nove if tuple(r) not in stare_set]

# ============================================================
# 🌐  SELENIUM – sdílené přihlášení
# ============================================================


def vytvor_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    if HEADLESS:
        options.add_argument("--headless=new")  # moderní headless režim

    # Automaticky najde Chrome/Chromium binárku
    for cesta in [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium"
    ]:
        if os.path.exists(cesta):
            options.binary_location = cesta
            log.info(f"🌐 Chrome/Chromium nalezen: {cesta}")
            break
    else:
        log.warning("⚠️ Chrome/Chromium nenalezen na standardních cestách, zkouším výchozí...")

    return webdriver.Chrome(options=options)

# ============================================================
# 🌐  SELENIUM – SUPLOVÁNÍ
# ============================================================

def zmen_datum(driver, wait, datum: date) -> bool:
    datum_str = datum.strftime("%d.%m.%Y")
    for pokus in range(1):
        pole = wait.until(EC.presence_of_element_located((By.ID, "DateEdit_I")))
        pole.click()
        time.sleep(0.3)
        pole.send_keys(Keys.CONTROL + "a")
        pole.send_keys(Keys.DELETE)
        time.sleep(0.2)
        pole.send_keys(datum_str)
        time.sleep(0.2)
        pole.send_keys(Keys.TAB)
        time.sleep(2)

        pole = wait.until(EC.presence_of_element_located((By.ID, "DateEdit_I")))
        hodnota = pole.get_attribute("value") or ""
        if datum_str in hodnota:
            log.info(f"📅 Datum → {datum_str}")
            return True

        log.warning(f"⚠️  Datum nesedí (pokus {pokus+1}/3): pole={hodnota!r}, očekáváno={datum_str}")

    # Poslední pokus přes JavaScript
    log.warning("⚠️  Zkouším nastavit datum přes JavaScript...")
    driver.execute_script(
        "var el = document.getElementById('DateEdit_I');"
        "el.value = arguments[0];"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "el.dispatchEvent(new Event('blur',   {bubbles:true}));",
        datum_str
    )
    time.sleep(2)
    hodnota = driver.find_element(By.ID, "DateEdit_I").get_attribute("value") or ""
    if datum_str in hodnota:
        log.info(f"📅 Datum nastaven přes JS → {datum_str}")
        return True

    log.error(f"❌ Datum se nepodařilo nastavit na {datum_str}, přeskakuji.")
    return False


def nacti_tabulku_suplovani(driver, datum: date) -> list:
    soup   = BeautifulSoup(driver.page_source, "html.parser")
    tables = soup.select("table.datagrid")

    suplovani_table = None
    for table in tables:
        th = table.find("th")
        if th and "Změny v rozvrzích učitelů" in th.get_text():
            suplovani_table = table
            break

    if suplovani_table is None:
        log.warning(f"[{datum.strftime('%d.%m.%Y')}] Tabulka suplování nenalezena.")
        return []

    rows      = suplovani_table.select("tbody > tr")[1:]
    datum_str = datum.strftime("%d.%m.%Y")
    data      = []

    for row in rows:
        jmeno = row.find("td").get_text(strip=True)
        inner = row.find("table", class_="inner")
        if inner:
            for ir in inner.find_all("tr"):
                cells = ir.find_all("td")
                if len(cells) < 6:
                    continue
                hodina   = cells[0].get_text(strip=True)
                span     = cells[1].find("span")
                typ      = span["title"].replace("\n", " ").strip() if span and span.get("title") else cells[1].get_text(strip=True)
                predmet  = cells[2].get_text(strip=True)
                trida    = cells[3].get_text(strip=True)
                poznamka = cells[5].get_text(strip=True) if len(cells) > 5 else ""
                data.append([datum_str, jmeno, hodina, typ, predmet, trida, poznamka])

    log.info(f"📄 [{datum_str}] Suplování: {len(data)} záznamů.")
    return data


def nacti_suplovani() -> list:
    log.info("🌐 Načítám suplování...")
    driver = vytvor_driver()
    wait   = WebDriverWait(driver, 20)
    try:
        driver.get(SUPLOVANI_URL)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(2)

        dnes   = date.today()
        zitrek = dnes + timedelta(days=1)
        data   = nacti_tabulku_suplovani(driver, dnes)

        if zmen_datum(driver, wait, zitrek):
            data += nacti_tabulku_suplovani(driver, zitrek)
        else:
            log.warning(f"⚠️  Zítřejší suplování ({zitrek.strftime('%d.%m.%Y')}) přeskočeno.")

        return data
    except Exception as e:
        raise RuntimeError(f"Suplování: {e}") from e
    finally:
        driver.quit()


# ============================================================
# 📬  SOUHRNNÝ REPORT → ADMIN
# ============================================================

def _barva_poznamky(poznamka: str) -> str:
    """Vrátí barvu pozadí řádku podle typu problému."""
    if "Nezapsaná" in poznamka:
        return "#ffd6d6"   # červená – nejzávažnější
    if "téma" in poznamka:
        return "#fff3cd"   # žlutá
    if "číslo" in poznamka:
        return "#d6eaff"   # modrá
    return "#f0f0f0"

# ============================================================
# 🔄  HLAVNÍ ÚLOHA – SUPLOVÁNÍ
# ============================================================

def zkontroluj_a_odesli() -> None:
    log.info("=" * 55)
    log.info("🔄 Spouštím kontrolu suplování...")

    try:
        with open(KONTAKTY_SOUBOR, encoding="utf-8") as f:
            kontakty = json.load(f)
        email_dict = {k["jmeno"]: k["email"] for k in kontakty}
    except Exception as e:
        zaznamen_chybu(f"Nelze načíst kontakty: {e}")
        return

    try:
        nova_data = nacti_suplovani()
    except Exception as e:
        zaznamen_chybu(str(e))
        return

    if not nova_data:
        log.warning("⚠️  Suplování: žádná data, přeskakuji.")
        return

    stara_data = nacti_cache()
    zmeny      = najdi_zmeny(stara_data, nova_data)

    uspesne_zaznamy = []
    if not zmeny:
        log.info("✅ Suplování: žádné změny.")
    else:
        log.info(f"🆕 {len(zmeny)} nových záznamů – odesílám e-maily.")
        uspesne_zaznamy = rozesli_emaily_suplovani(zmeny, email_dict)
        if uspesne_zaznamy:
            uloz_do_historie(uspesne_zaznamy)

    # Aktualizuj cache jen s úspěšnými záznamy
    nova_cache = stara_data + uspesne_zaznamy
    uloz_cache(nova_cache)
    log.info("💾 Cache aktualizována.")
    reset_chyb()

# ============================================================
# ♻️  TÝDENNÍ ÚDRŽBA
# ============================================================

def tydenna_udrzba() -> None:
    log.info("♻️  Týdenní údržba – uvolňuji paměť...")
    gc.collect()
    log.info("✅ Paměť uvolněna.")

# ============================================================
# ⏰  SPUŠTĚNÍ
# ============================================================

if __name__ == "__main__":
    log.info("🚀 Program spuštěn.")
    log.info(f"   Odesílatel : {ODESILATEL}")
    log.info(f"   Admin email: {ADMIN_EMAIL}")
    log.info(f"   Kontakty   : {KONTAKTY_SOUBOR}")
    log.info(f"   Max chyb   : {MAX_CHYB}")

    # Suplování hned při startu
    try:
        zkontroluj_a_odesli()
    except Exception as e:
        chyba = f"Chyba při prvním spuštění:\n{e}"
        log.critical(chyba)
        posli_email(ADMIN_EMAIL, chyba, predmet="🚨 Chyba při startu – suplování")

    # Pondělí – pátek: každých 15 minut od 6:00 do 12:00
    for minuty in range(6 * 60, 12 * 60 + 1, 15):
        cas = f"{minuty // 60:02d}:{minuty % 60:02d}"
        schedule.every().monday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().tuesday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().wednesday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().thursday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().friday.at(cas).do(zkontroluj_a_odesli)

    # Pondělí – pátek: každé 2 hodiny od 12:00 do 21:00
    for minuty in range(12 * 60, 21 * 60 + 1, 120):
        cas = f"{minuty // 60:02d}:{minuty % 60:02d}"
        schedule.every().monday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().tuesday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().wednesday.at(cas).do(zkontroluj_a_odesli)
        schedule.every().thursday.at(cas).do(zkontroluj_a_odesli)

    # Neděle: každé 2 hodiny od 12:00 do 21:00
    for minuty in range(12 * 60, 21 * 60 + 1, 120):
        cas = f"{minuty // 60:02d}:{minuty % 60:02d}"
        schedule.every().sunday.at(cas).do(zkontroluj_a_odesli)

    # Týdenní údržba paměti – každé pondělí v 6:00
    schedule.every().monday.at("06:00").do(tydenna_udrzba)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except Exception as e:
        chyba = f"Neočekávané selhání hlavní smyčky:\n{e}"
        log.critical(chyba)
        posli_email(ADMIN_EMAIL, chyba, predmet="🚨 Kritické selhání – program zastaven")
        raise
