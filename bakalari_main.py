import json
import os
import time
import smtplib
import logging
import schedule
import gc
import re
#t
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService

from selenium.webdriver.chrome.options import Options as ChromeOptions

from collections import defaultdict
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from email.message import EmailMessage
from typing import Optional

# ============================================================
# ⚙️  KONFIGURACE
# ============================================================

LOGIN_URL        = "https://zsamsbohuslavice.bakalari.cz/login"
SUPLOVANI_URL    = "https://zsamsbohuslavice.bakalari.cz/next/pluginDirectLinkIFrame.aspx?id=2"
TRIDNI_KNIHA_URL = "https://zsamsbohuslavice.bakalari.cz/ClassTeacherWork/ClassbookCheck"

CACHE_SOUBOR          = "cache_suplovani.json"
KONTAKTY_SOUBOR       = "kontakty.json"
TRIDNI_UCITELE_SOUBOR = "tridni_ucitele.json"
MAX_CHYB              = 5

# Hodnoty tříd v <select>
TRIDY_HODNOTY = ["0L", "0M", "0N", "0O", "0P", "0Q", "0R", "0W", "0U", "0V", "0S"]

load_dotenv()
USERNAME    = os.getenv("USERNAME2")
PASSWORD    = os.getenv("PASSWORD")
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
        RotatingFileHandler("suplovani.log", maxBytes=5*1024*1024,
                            backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ============================================================
# 🔢  POČÍTADLO CHYB
# ============================================================

chyby_v_rade = 0


def zaznamen_chybu(zprava: str) -> None:
    global chyby_v_rade
    chyby_v_rade += 1
    log.error(f"Chyba #{chyby_v_rade}: {zprava}")
    if chyby_v_rade >= MAX_CHYB:
        log.critical(f"🚨 Selhání {MAX_CHYB}x za sebou – informuji admina.")
        posli_email(
            ODESILATEL,
            f"🚨 Program selhal {MAX_CHYB}x za sebou!\n\nPoslední chyba:\n{zprava}\n\nZkontroluj server.",
            predmet="🚨 Chyba programu suplování"
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
        log.error(f"❌ Chyba odesílání → {prijemce}: {e}")
        return False

# ============================================================
# 📬  E-MAILY – SUPLOVÁNÍ
# ============================================================

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


def rozesli_emaily_suplovani(zmenena_data: list, email_dict: dict) -> None:
    skupiny: dict = defaultdict(list)
    for r in zmenena_data:
        skupiny[r[1]].append(r)

    bez_emailu, odeslano = [], 0
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

    log.info(f"📊 Odesláno: {odeslano} e-mailů")
    if bez_emailu:
        log.warning(f"⚠️  Bez e-mailu: {', '.join(bez_emailu)}")

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
'''
def vytvor_driver() -> webdriver.Firefox:
    options = FirefoxOptions()
    #options.add_argument("--headless")

    # Automaticky najde Firefox binárku na běžných cestách
    for cesta in ["/usr/bin/firefox", "/snap/bin/firefox", "/usr/lib/firefox/firefox"]:
        if os.path.exists(cesta):
            options.binary_location = cesta
            log.info(f"🦊 Firefox nalezen: {cesta}")
            break
    else:
        log.warning("⚠️  Firefox nenalezen na standardních cestách, zkouším výchozí...")

    return webdriver.Firefox(options=options)
'''

def vytvor_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    #options.add_argument("--headless=new")  # moderní headless režim

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



def prihlasit_se(driver, wait) -> None:
    driver.get(LOGIN_URL)
    wait.until(EC.presence_of_element_located((By.ID, "username"))).send_keys(USERNAME)
    driver.find_element(By.ID, "password").send_keys(PASSWORD, Keys.RETURN)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    time.sleep(2)
    log.info("🔑 Přihlášení OK.")

# ============================================================
# 🌐  SELENIUM – SUPLOVÁNÍ
# ============================================================

def zmen_datum(driver, wait, datum: date) -> None:
    datum_str = datum.strftime("%d.%m.%Y")
    pole = wait.until(EC.presence_of_element_located((By.ID, "DateEdit_I")))
    pole.click()
    pole.send_keys(Keys.CONTROL + "a")
    pole.send_keys(Keys.DELETE)
    pole.send_keys(datum_str)
    pole.send_keys(Keys.TAB)
    time.sleep(2)
    log.info(f"📅 Datum → {datum_str}")


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
        prihlasit_se(driver, wait)
        driver.get(SUPLOVANI_URL)
        wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, "cphmain_iframe")))
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(2)

        dnes   = date.today()
        zitrek = dnes + timedelta(days=1)
        data   = nacti_tabulku_suplovani(driver, dnes)
        zmen_datum(driver, wait, zitrek)
        data  += nacti_tabulku_suplovani(driver, zitrek)
        return data
    except Exception as e:
        raise RuntimeError(f"Suplování: {e}") from e
    finally:
        driver.quit()


# ============================================================
# 🔤  POMOCNÉ FUNKCE – JMÉNA A TITULY
# ============================================================

# Akademické a jiné tituly které se v Bakalářích zobrazují za jménem
TITULY = [
    "Mgr.", "Bc.", "Ing.", "PhDr.", "RNDr.", "PaedDr.", "ThDr.",
    "JUDr.", "MUDr.", "PhD.", "Ph.D.", "CSc.", "DiS.", "MBA",
    "Ing. arch.", "doc.", "prof.",
]


def odeber_tituly(jmeno: str) -> str:
    """
    Odebere akademické tituly ze jména kdekoliv (začátek, konec, uprostřed).
    Např. 'Snášelová Miroslava Mgr.' → 'Snášelová Miroslava'
          'Mgr. Jan Novák'           → 'Jan Novák'
    Tituly jsou seřazeny od nejdelšího, aby 'Ing. arch.' nebyl rozbit na 'Ing.'
    """
    jmeno = jmeno.strip()
    for titul in sorted(TITULY, key=len, reverse=True):
        # Hledáme titul obklopený mezerami nebo na začátku/konci řetězce
        pattern = r'(?:^|\s)' + re.escape(titul) + r'(?=\s|$)'
        jmeno = re.sub(pattern, ' ', jmeno)
    return re.sub(r'\s+', ' ', jmeno).strip()


def najdi_email(jmeno_s_tituly: str, email_dict: dict) -> Optional[str]:
    """
    Hledá e-mail učitele v email_dict (kde jsou klíče BEZ titulů).
    Zkouší přesnou shodu → shodu bez titulů → case-insensitive shodu bez titulů.
    """
    # 1) Přesná shoda (pokud má email_dict klíče s tituly)
    email = email_dict.get(jmeno_s_tituly)
    if email:
        return email
    # 2) Bez titulů
    bez = odeber_tituly(jmeno_s_tituly)
    email = email_dict.get(bez)
    if email:
        return email
    # 3) Case-insensitive, obě strany bez titulů
    bez_lower = bez.lower()
    for klic, mail in email_dict.items():
        if odeber_tituly(klic).lower() == bez_lower:
            return mail
    return None


def prvni_jmeno(jmeno_s_tituly: str) -> str:
    """
    Vrátí křestní jméno pro oslovení.
    Bakaláři ukládají jméno jako 'Příjmení Křestní [Titul]' – bereme druhý token.
    """
    casti = odeber_tituly(jmeno_s_tituly).split()
    if len(casti) >= 2:
        return casti[1]
    return casti[0] if casti else jmeno_s_tituly

# ============================================================
# 🛠️  POMOCNÉ FUNKCE – TŘÍDY A DATUM
# ============================================================

def normalizuj_tridu(skupina: str) -> str:
    """
    Z '1.A (celá)', '7. (Dív)', '5.B (celá)' → '1.A', '7.', '5.B'.
    """
    return re.sub(r'\s*\(.*?\)', '', skupina).strip()


def hledej_tridni(trida: str, tridni_ucitele: dict) -> Optional[str]:
    """Fuzzy hledání třídního učitele – ignoruje tečky a velikost písmen."""
    trida_norm = trida.lower().replace(" ", "").replace(".", "")
    for klic, ucitel in tridni_ucitele.items():
        if klic.lower().replace(" ", "").replace(".", "") == trida_norm:
            return ucitel
    return None


def datum_pro_razeni(datum_str: str) -> date:
    """'7. 1. 2026' nebo '7.1.2026' → date objekt pro řazení."""
    try:
        cisla = re.findall(r'\d+', datum_str)
        if len(cisla) == 3:
            return date(int(cisla[2]), int(cisla[1]), int(cisla[0]))
    except Exception:
        pass
    return date.min

# ============================================================
# 🌐  SELENIUM – TŘÍDNICE
# ============================================================

def nastav_parametry_kontroly_ko(driver, datum_zacatek: str, datum_konec: str) -> None:
    """
    Nastaví všechny parametry kontroly třídnice přes Knockout.js observable.

    Z HTML stránky víme že KO model je ClassbookCheck s ControlSettings:
      - DateFrom / DateTo       → ISO datum string
      - ControlLessonNumber     → bool (číslo hodiny)
      - ControlTopic            → bool (téma hodiny)
      - ControlNote             → bool (poznámka – chceme False)
      - ClassesIds              → pole kódů tříd

    Datum vstup je ve formátu 'D.M.YYYY', převedeme na ISO pro KO.
    """
    hodnoty_js = json.dumps(TRIDY_HODNOTY)

    driver.execute_script(f"""
        // ── Pomocná funkce: 'D.M.YYYY' → ISO string ─────────
        function datumNaISO(str) {{
            var casti = str.split('.');
            var d = parseInt(casti[0]), m = parseInt(casti[1]) - 1, y = parseInt(casti[2]);
            return new Date(y, m, d).toISOString();
        }}

        // ── Najdi KO model ────────────────────────────────────
        var el = document.getElementById('_classbookCheckContent');
        if (!el) {{ console.error('KO element nenalezen'); return; }}
        var vm = ko.dataFor(el);
        if (!vm) {{ console.error('KO viewmodel nenalezen'); return; }}
        var cs = vm.ControlSettings;

        // ── Datumy ────────────────────────────────────────────
        var isoOd = datumNaISO('{datum_zacatek}');
        var isoDo = datumNaISO('{datum_konec}');
        cs.DateFrom(isoOd);
        cs.DateTo(isoDo);

        // ── Třídy ─────────────────────────────────────────────
        var tridy = {hodnoty_js};
        cs.ClassesIds(tridy);

        // ── Checkboxy ─────────────────────────────────────────
        cs.ControlLessonNumber(true);   // číslo hodiny – zapnout
        cs.ControlTopic(true);          // téma hodiny  – zapnout
        cs.ControlNote(false);          // poznámka     – vypnout

        console.log('KO parametry nastaveny OK');
    """)
    log.info(f"✅ KO parametry nastaveny: {datum_zacatek} – {datum_konec}, třídy: {len(TRIDY_HODNOTY)} ks.")
    time.sleep(1)


def klikni_proved_kontrolu(driver, wait) -> None:
    """Klikne na tlačítko 'Provést kontrolu' (nesmí mít třídu 'disabled')."""
    tlacitko = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//button[contains(., 'Provést kontrolu') and not(contains(@class, 'disabled'))]")
    ))
    driver.execute_script("arguments[0].click();", tlacitko)
    log.info("🖱️  Kliknuto na 'Provést kontrolu'.")
    time.sleep(4)


def nastav_200_zaznamu(driver, wait) -> None:
    """Klikne na tlačítko '200' v dx-page-sizes pro zobrazení max. záznamů na stránce."""
    try:
        tlacitko = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//div[contains(@class,'dx-page-size') and text()='200']")
        ))
        driver.execute_script("arguments[0].click();", tlacitko)
        log.info("📄 Nastaven počet záznamů na stránce: 200.")
        time.sleep(2)
    except Exception as e:
        log.warning(f"⚠️  Nepodařilo se nastavit 200 záznamů: {e}")


def nacti_vysledky_tridnice(driver) -> list:
    """
    Parsuje DevExtreme DataGrid (dx-datagrid-rowsview → dx-data-row).

    Sloupce (dle HTML):
      0: Datum | 1: Den v týdnu | 2: Hodina | 3: Předmět
      4: Skupina | 5: Vyučující | 6: Poznámka | 7: Zapsat (tlačítko)

    Příklady poznámky: 'Chybí téma hodiny', 'Chybí číslo hodiny', 'Nezapsaná hodina'
    """
    soup = BeautifulSoup(driver.page_source, "html.parser")
    rowsview = soup.select_one("div.dx-datagrid-rowsview")
    if not rowsview:
        log.warning("⚠️  dx-datagrid-rowsview nenalezena.")
        return []

    zaznamy = []
    for radek in rowsview.select("tr.dx-data-row"):
        bunky = radek.find_all("td")
        if len(bunky) < 7:
            continue
        zaznam = {
            "datum"     : bunky[0].get_text(strip=True),
            "den"       : bunky[1].get_text(strip=True),
            "hodina"    : bunky[2].get_text(strip=True),
            "predmet"   : bunky[3].get_text(strip=True),
            "skupina"   : bunky[4].get_text(strip=True),
            "vyucujici" : bunky[5].get_text(strip=True),
            "poznamka"  : bunky[6].get_text(strip=True),
        }
        if zaznam["datum"] or zaznam["vyucujici"]:
            zaznamy.append(zaznam)

    log.info(f"📊 Třídnice: {len(zaznamy)} problémových záznamů.")
    return zaznamy


# ============================================================
# 📬  E-MAILY – TŘÍDNICE: učitelé
# ============================================================

def rozesli_emaily_ucitelum(zaznamy: list, email_dict: dict) -> None:
    """Každému vyučujícímu pošle jeho nezapsané hodiny (seřazené datum → hodina)."""
    podle_ucitele: dict = defaultdict(list)
    for z in zaznamy:
        podle_ucitele[z["vyucujici"]].append(z)

    odeslano, bez_emailu = 0, []

    for ucitel, seznam in sorted(podle_ucitele.items()):
        email = najdi_email(ucitel, email_dict)
        if not email:
            bez_emailu.append(ucitel)
            continue

        seznam_s = sorted(
            seznam,
            key=lambda z: (
                datum_pro_razeni(z["datum"]),
                int(z["hodina"]) if z["hodina"].isdigit() else 0
            )
        )

        jmeno_kratke = prvni_jmeno(ucitel)
        dnes_str = date.today().strftime("%d.%m.%Y")

        # Prostý text
        radky_txt = "".join(
            f"  • {z['datum']} ({z['den']}) | {z['hodina']}. hod."
            f" | {z['predmet']} | {z['skupina']} | {z['poznamka']}\n"
            for z in seznam_s
        )
        zprava_txt = (
            f"Ahoj {jmeno_kratke},\n\n"
            f"v třídní knize máš následující nezapsané nebo neúplné hodiny:\n\n"
            f"{radky_txt}\n"
            f"Prosím doplň chybějící záznamy co nejdříve.\n\nHezký den!\n"
        )

        # HTML
        radky_html = ""
        for z in seznam_s:
            barva = _barva_poznamky(z["poznamka"])
            radky_html += (
                f'<tr style="background:{barva};">'
                f'<td>{z["datum"]}</td><td>{z["den"]}</td>'
                f'<td style="text-align:center;">{z["hodina"]}.</td>'
                f'<td>{z["predmet"]}</td><td>{z["skupina"]}</td>'
                f'<td><b>{z["poznamka"]}</b></td>'
                f'</tr>\n'
            )
        html = f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8">
<style>
body{{font-family:Arial,sans-serif;font-size:14px;color:#222}}
h2{{font-size:17px;font-weight:500;margin:0 0 4px}}
.sub{{font-size:13px;color:#666;margin:0 0 16px}}
.warn{{background:#fff3cd;border-left:4px solid #e6a817;padding:10px 14px;
       border-radius:4px;margin-bottom:16px;font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#2c3e50;color:#fff;padding:7px 9px;text-align:left;font-weight:500}}
td{{padding:6px 9px;border-bottom:0.5px solid #ddd}}
.footer{{font-size:11px;color:#aaa;margin-top:14px;padding-top:10px;border-top:0.5px solid #e0e0e0}}
</style></head><body>
<h2>Nezapsané hodiny v třídní knize – {dnes_str}</h2>
<p class="sub">Ahoj {jmeno_kratke}, prosím doplň níže uvedené záznamy co nejdříve.</p>
<div class="warn">Celkem <b>{len(seznam_s)}</b> nezapsaných nebo neúplných hodin.</div>
<table>
  <thead><tr>
    <th>Datum</th><th>Den</th><th>Hod.</th>
    <th>Předmět</th><th>Skupina</th><th>Problém</th>
  </tr></thead>
  <tbody>
{radky_html}  </tbody>
</table>
<p class="footer">Automaticky vygenerováno systémem Bakalátor 3000 – suplování &amp; třídnice. Při jakýchkoli potížích kontaktujte Radka.</p>
</body></html>"""

        predmet = (
            f"⚠️  Nezapsané hodiny – třídní kniha"
            f" ({len(seznam_s)} zázn.) – {dnes_str}"
        )
        if posli_email(email, zprava_txt, predmet=predmet, html=html):
            odeslano += 1

    log.info(f"📊 Třídnice – učitelé: odesláno {odeslano} e-mailů")
    if bez_emailu:
        log.warning(f"⚠️  Bez e-mailu (učitelé): {', '.join(bez_emailu)}")


# ============================================================
# 📬  E-MAILY – TŘÍDNICE: třídní učitelé
# ============================================================

def rozesli_emaily_tridnim(zaznamy: list, email_dict: dict,
                            tridni_ucitele: dict) -> None:
    """
    Každé třídní učitelce pošle přehled problémů v její třídě
    (seřazené datum → hodina).
    """
    podle_tridy: dict = defaultdict(list)
    for z in zaznamy:
        podle_tridy[normalizuj_tridu(z["skupina"])].append(z)

    odeslano, bez_tridni, bez_emailu = 0, [], []

    for trida, seznam in sorted(podle_tridy.items()):
        tridni = tridni_ucitele.get(trida) or hledej_tridni(trida, tridni_ucitele)
        if not tridni:
            bez_tridni.append(trida)
            continue

        email = najdi_email(tridni, email_dict)
        if not email:
            bez_emailu.append(f"{tridni} ({trida})")
            continue

        seznam_s = sorted(
            seznam,
            key=lambda z: (
                datum_pro_razeni(z["datum"]),
                int(z["hodina"]) if z["hodina"].isdigit() else 0
            )
        )

        jmeno_kratke = prvni_jmeno(tridni)
        dnes_str = date.today().strftime("%d.%m.%Y")

        radky_txt = "".join(
            f"  • {z['datum']} ({z['den']}) | {z['hodina']}. hod."
            f" | {z['predmet']} | {z['vyucujici']} | {z['poznamka']}\n"
            for z in seznam_s
        )
        zprava_txt = (
            f"Ahoj {jmeno_kratke},\n\n"
            f"ve tvé třídě ({trida}) byly nalezeny"
            f" nezapsané nebo neúplné hodiny ({len(seznam_s)} záznamů):\n\n"
            f"{radky_txt}\n"
            f"Prosím kontaktuj příslušné vyučující, aby záznamy doplnili.\n\nHezký den!\n"
        )

        radky_html = ""
        for z in seznam_s:
            barva = _barva_poznamky(z["poznamka"])
            radky_html += (
                f'<tr style="background:{barva};">'
                f'<td>{z["datum"]}</td><td>{z["den"]}</td>'
                f'<td style="text-align:center;">{z["hodina"]}.</td>'
                f'<td>{z["predmet"]}</td><td>{z["vyucujici"]}</td>'
                f'<td><b>{z["poznamka"]}</b></td>'
                f'</tr>\n'
            )
        # Přehled učitelů pro třídní
        podle_ucitele_tridni: dict = defaultdict(int)
        for z in seznam_s:
            podle_ucitele_tridni[z["vyucujici"]] += 1
        ucitele_tridni_html = "".join(
            f'<tr><td>{j}</td><td style="text-align:center;font-weight:bold;">{p}</td></tr>\n'
            for j, p in sorted(podle_ucitele_tridni.items(), key=lambda x: -x[1])
        )

        html = f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8">
<style>
body{{font-family:Arial,sans-serif;font-size:14px;color:#222}}
h2{{font-size:17px;font-weight:500;margin:0 0 4px}}
h3{{font-size:14px;font-weight:500;margin:16px 0 6px;color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:3px}}
.sub{{font-size:13px;color:#666;margin:0 0 14px}}
.warn{{background:#fff3cd;border-left:4px solid #e6a817;padding:9px 13px;border-radius:4px;margin-bottom:14px;font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#2c3e50;color:#fff;padding:7px 9px;text-align:left;font-weight:500}}
td{{padding:6px 9px;border-bottom:0.5px solid #ddd}}
.tbl-u{{max-width:380px}}
.tbl-u tr:nth-child(even) td{{background:#f5f5f5}}
.footer{{font-size:11px;color:#aaa;margin-top:14px;padding-top:10px;border-top:0.5px solid #e0e0e0}}
</style></head><body>
<h2>Třídní kniha – třída {trida} – {dnes_str}</h2>
<p class="sub">Ahoj {jmeno_kratke}, ve tvé třídě byly nalezeny nezapsané nebo neúplné hodiny.</p>
<div class="warn">Celkem <b>{len(seznam_s)}</b> záznamů vyžaduje doplnění.</div>

<h3>Přehled vyučujících</h3>
<table class="tbl-u">
  <thead><tr><th>Vyučující</th><th style="text-align:center;">Počet problémů</th></tr></thead>
  <tbody>
{ucitele_tridni_html}  </tbody>
</table>

<h3>Detail záznamů</h3>
<table>
  <thead><tr>
    <th>Datum</th><th>Den</th><th>Hod.</th>
    <th>Předmět</th><th>Vyučující</th><th>Problém</th>
  </tr></thead>
  <tbody>
{radky_html}  </tbody>
</table>
<p class="footer">Automaticky vygenerováno systémem Bakalátor 3000 – suplování &amp; třídnice. Při jakýchkoli potížích kontaktujte Radka.</p>
</body></html>"""

        predmet = (
            f"⚠️  Třídní kniha – třída {trida}"
            f" ({len(seznam_s)} probl.) – {dnes_str}"
        )
        if posli_email(email, zprava_txt, predmet=predmet, html=html):
            odeslano += 1

    log.info(f"📊 Třídnice – třídní uč.: odesláno {odeslano} e-mailů")
    if bez_tridni:
        log.warning(f"⚠️  Nenalezena třídní pro: {', '.join(bez_tridni)}")
    if bez_emailu:
        log.warning(f"⚠️  Bez e-mailu (třídní): {', '.join(bez_emailu)}")


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


def posli_souhrnny_report(zaznamy: list) -> None:
    dnes = date.today().strftime("%d.%m.%Y")
    zaznamy_s = sorted(
        zaznamy,
        key=lambda z: (
            datum_pro_razeni(z["datum"]),
            normalizuj_tridu(z["skupina"]),
            int(z["hodina"]) if z["hodina"].isdigit() else 0
        )
    )

    # ── Statistiky ──────────────────────────────────────────
    nezapsane   = sum(1 for z in zaznamy if "Nezapsaná" in z["poznamka"])
    chybi_tema  = sum(1 for z in zaznamy if "téma"      in z["poznamka"])
    chybi_cislo = sum(1 for z in zaznamy if "číslo"     in z["poznamka"])

    # ── Přehled učitelů: jméno → počet problémů ────────────
    podle_ucitele: dict = defaultdict(int)
    for z in zaznamy:
        podle_ucitele[z["vyucujici"]] += 1
    ucitele_sorted = sorted(podle_ucitele.items(), key=lambda x: -x[1])

    # ── Prostý text (fallback) ──────────────────────────────
    ucitele_txt = "".join(
        f"  {jmeno}: {pocet} záznamů\n"
        for jmeno, pocet in ucitele_sorted
    )
    radky_txt = "".join(
        f"  {z['datum']} | {z['hodina']}. hod. | {z['predmet']}"
        f" | {z['skupina']} | {z['vyucujici']} | {z['poznamka']}\n"
        for z in zaznamy_s
    )
    zprava_txt = (
        f"Týdenní kontrola třídnice – {dnes}\n"
        f"Celkem {len(zaznamy)} problémových záznamů\n\n"
        f"Přehled učitelů:\n{ucitele_txt}\n"
        f"Detail:\n{radky_txt}\n"
        f"E-maily byly odeslány příslušným učitelům a třídním učitelkám."
    )

    # ── HTML: tabulka učitelů ───────────────────────────────
    radky_ucitele_html = ""
    for jmeno, pocet in ucitele_sorted:
        radky_ucitele_html += (
            f'<tr>'
            f'<td>{jmeno}</td>'
            f'<td style="text-align:center;font-weight:bold;">{pocet}</td>'
            f'</tr>\n'
        )

    # ── HTML: detail záznamů ────────────────────────────────
    radky_html = ""
    for z in zaznamy_s:
        barva = _barva_poznamky(z["poznamka"])
        radky_html += (
            f'<tr style="background:{barva};">'
            f'<td>{z["datum"]}</td>'
            f'<td>{z["den"]}</td>'
            f'<td style="text-align:center;">{z["hodina"]}.</td>'
            f'<td>{z["predmet"]}</td>'
            f'<td>{z["skupina"]}</td>'
            f'<td>{z["vyucujici"]}</td>'
            f'<td><b>{z["poznamka"]}</b></td>'
            f'</tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 14px; color: #222; }}
  h2   {{ font-size: 18px; font-weight: 500; margin: 0 0 4px; color: #c0392b; }}
  h3   {{ font-size: 15px; font-weight: 500; margin: 20px 0 8px; color: #2c3e50; border-bottom: 2px solid #2c3e50; padding-bottom: 4px; }}
  .stats {{ display: flex; gap: 12px; margin: 14px 0; flex-wrap: wrap; }}
  .stat-box {{ padding: 10px 18px; border-radius: 8px; font-size: 14px; font-weight: bold; min-width: 110px; text-align: center; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }}
  th {{ background: #2c3e50; color: #fff; padding: 7px 10px; text-align: left; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }}
  .tbl-ucitele td:first-child {{ font-weight: 500; }}
  .tbl-ucitele tr:nth-child(even) td {{ background: #f5f5f5; }}
  tr:hover td {{ filter: brightness(0.97); }}
  .footer {{ margin-top: 16px; color: #999; font-size: 12px; border-top: 1px solid #e0e0e0; padding-top: 10px; }}
</style></head><body>

<h2>Souhrnný report třídnice – {dnes}</h2>
<p>Celkem <b>{len(zaznamy)}</b> problémových záznamů za aktuální školní rok.</p>

<div class="stats">
  <div class="stat-box" style="background:#ffd6d6;color:#7a2020;">Nezapsaná hodina<br>{nezapsane}</div>
  <div class="stat-box" style="background:#fff3cd;color:#7a5a00;">Chybí téma<br>{chybi_tema}</div>
  <div class="stat-box" style="background:#d6eaff;color:#1a4a80;">Chybí číslo hodiny<br>{chybi_cislo}</div>
</div>

<h3>Přehled učitelů</h3>
<table class="tbl-ucitele" style="max-width:420px;">
  <thead><tr><th>Vyučující</th><th style="text-align:center;">Počet problémů</th></tr></thead>
  <tbody>
{radky_ucitele_html}  </tbody>
</table>

<h3>Detail všech záznamů</h3>
<table>
  <thead><tr>
    <th>Datum</th><th>Den</th><th>Hod.</th>
    <th>Předmět</th><th>Skupina</th><th>Vyučující</th><th>Problém</th>
  </tr></thead>
  <tbody>
{radky_html}  </tbody>
</table>

<p class="footer">E-maily byly odeslány příslušným učitelům a třídním učitelkám.</p>
</body></html>"""

    posli_email(
        ADMIN_EMAIL,
        zprava_txt,
        predmet=f"Souhrnný report třídnice – {dnes} ({len(zaznamy)} problémů)",
        html=html
    )


# ============================================================
# 🔄  HLAVNÍ ÚLOHA – TŘÍDNICE
# ============================================================

def skolni_rok_zacatek() -> str:
    """
    Vrátí datum začátku aktuálního školního roku jako '1.9.YYYY'.
    Školní rok začíná 1.9. – pokud jsme před 1.9., vrátíme loňský rok.
    """
    dnes = date.today()
    rok = dnes.year if dnes.month >= 9 else dnes.year - 1
    return f"1.9.{rok}"





def zkontroluj_tridnici() -> None:
    log.info("=" * 55)
    log.info("📚 Spouštím týdenní kontrolu třídnice...")

    try:
        with open(KONTAKTY_SOUBOR, encoding="utf-8") as f:
            kontakty = json.load(f)
        email_dict = {k["jmeno"]: k["email"] for k in kontakty}
    except Exception as e:
        zaznamen_chybu(f"Nelze načíst kontakty: {e}")
        return

    try:
        with open(TRIDNI_UCITELE_SOUBOR, encoding="utf-8") as f:
            tridni_ucitele = {k.strip(): v.strip() for k, v in json.load(f).items()}
    except Exception as e:
        zaznamen_chybu(f"Nelze načíst třídní učitele: {e}")
        return

    datum_zacatek = skolni_rok_zacatek()
    dnes_dt = date.today()
    datum_konec = f"{dnes_dt.day}.{dnes_dt.month}.{dnes_dt.year}"  # např. 15.3.2026
    log.info(f"📅 Rozsah kontroly: {datum_zacatek} – {datum_konec}")

    driver = vytvor_driver()
    wait   = WebDriverWait(driver, 30)
    try:
        prihlasit_se(driver, wait)
        driver.get(TRIDNI_KNIHA_URL)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)

        # ── Všechny parametry najednou přes Knockout.js ───────
        nastav_parametry_kontroly_ko(driver, datum_zacatek, datum_konec)

        # ── Spuštění kontroly ──────────────────────────────
        klikni_proved_kontrolu(driver, wait)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(2)

        nastav_200_zaznamu(driver, wait)
        zaznamy = nacti_vysledky_tridnice(driver)
    except Exception as e:
        zaznamen_chybu(f"Třídnice: {e}")
        return
    finally:
        driver.quit()

    if not zaznamy:
        log.info("✅ Třídnice: žádné problémy.")
        posli_email(
            ADMIN_EMAIL,
            f"Týdenní kontrola třídnice – {date.today().strftime('%d.%m.%Y')}\n\n"
            f"✅ Vše v pořádku – žádné problémy nenalezeny.\n",
            predmet=f"✅ Třídnice OK – {date.today().strftime('%d.%m.%Y')}"
        )
        reset_chyb()
        return

    rozesli_emaily_ucitelum(zaznamy, email_dict)
    rozesli_emaily_tridnim(zaznamy, email_dict, tridni_ucitele)
    posli_souhrnny_report(zaznamy)
    reset_chyb()


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

    if not zmeny:
        log.info("✅ Suplování: žádné změny.")
    else:
        log.info(f"🆕 {len(zmeny)} nových záznamů – odesílám e-maily.")
        rozesli_emaily_suplovani(zmeny, email_dict)

    uloz_cache(nova_data)
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
    log.info(f"   Třídní uč. : {TRIDNI_UCITELE_SOUBOR}")
    log.info(f"   Max chyb   : {MAX_CHYB}")

    # Suplování hned při startu
    zkontroluj_tridnici()
    zkontroluj_a_odesli()

    # Suplování – každých 15 minut od 6:00 do 12:00
    for minuty in range(6 * 60, 12 * 60 + 1, 15):
        cas = f"{minuty // 60:02d}:{minuty % 60:02d}"
        schedule.every().day.at(cas).do(zkontroluj_a_odesli)

    # Suplování – každé 2 hodiny od 12:00 do 21:00
    for minuty in range(12 * 60, 21 * 60 + 1, 120):
        cas = f"{minuty // 60:02d}:{minuty % 60:02d}"
        schedule.every().day.at(cas).do(zkontroluj_a_odesli)

    # Třídnice – každou neděli v 14:00
    schedule.every().sunday.at("14:00").do(zkontroluj_tridnici)

    # Týdenní údržba paměti – každé pondělí v 6:00
    schedule.every().monday.at("06:00").do(tydenna_udrzba)

    while True:
        schedule.run_pending()
        time.sleep(30)