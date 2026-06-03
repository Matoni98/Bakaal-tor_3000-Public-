import json
import os
import time
import smtplib
import logging
import re
#t
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.webdriver.chrome.options import Options as ChromeOptions

from collections import defaultdict
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from email.message import EmailMessage
from typing import Optional

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
TRIDNI_KNIHA_URL = "https://zsamsbohuslavice.bakalari.cz/ClassTeacherWork/ClassbookCheck"

KONTAKTY_SOUBOR       = "kontakty.json"
TRIDNI_UCITELE_SOUBOR = "tridni_ucitele.json"
MAX_CHYB              = 1
chyby_v_rade = 0

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
        RotatingFileHandler(os.path.join(logs_dir, "tridnice.log"), maxBytes=5 * 1024 * 1024,
                            backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ============================================================
# 🔢  POČÍTADLO CHYB
# ============================================================


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
    import time
    max_pokusu = 3
    for pokus in range(1, max_pokusu + 1):
        try:
            zprava = EmailMessage()
            zprava["Subject"] = predmet
            zprava["From"]    = ODESILATEL
            zprava["To"]      = prijemce
            zprava.set_content(zprava_text)
            if html:
                zprava.add_alternative(html, subtype="html")
            with smtplib.SMTP_SSL("smtp.webzdarma.cz", 465) as smtp:
                smtp.login(ODESILATEL, HESLO)
                smtp.send_message(zprava)
            log.info(f"✅ E-mail odeslán → {prijemce}")
            return True
        except Exception as e:
            log.error(f"❌ Chyba odesílání → {prijemce} (pokus {pokus}/{max_pokusu}): {e}")
            if pokus < max_pokusu:
                time.sleep(10)  # čekej 10 sekund před dalším pokusem
    # Po všech pokusech selhalo
    log.error(f"❌ E-mail se nepodařilo odeslat po {max_pokusu} pokusech → {prijemce}")
    return False

# ============================================================
# 🌐  SELENIUM – sdílené přihlášení
# ============================================================

def vytvor_driver() -> webdriver.Chrome:
    options = ChromeOptions()
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


def prihlasit_se(driver, wait) -> None:
    driver.get(LOGIN_URL)
    wait.until(EC.presence_of_element_located((By.ID, "username"))).send_keys(USERNAME)
    driver.find_element(By.ID, "password").send_keys(PASSWORD, Keys.RETURN)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    time.sleep(2)
    log.info("🔑 Přihlášení OK.")

# ============================================================
# 🔤  POMOCNÉ FUNKCE – JMÉNA A TITULY
# ============================================================

# Akademické a jiné tituly které se v Bakalářích zobrazují za jménem
TITULY = [
    "Mgr.", "Bc.", "Ing.", "PhDr.", "RNDr.", "PaedDr.", "ThDr.",
    "JUDr.", "MUDr.", "PhD.", "Ph.D.", "CSc.", "DiS.", "MBA",
    "Ing. arch.", "doc.", "prof.", "Dis",
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
def je_prvni_spusteni_v_mesici() -> bool:
    """První neděle v měsíci = den < 8."""
    return date.today().day < 8

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
# 📬  E-MAILY – TŘÍDNICE: třídní učitelé (sloučený e-mail)
# ============================================================

def rozesli_emaily_tridnim(zaznamy: list, email_dict: dict,
                            tridni_ucitele: dict,
                            prvni_v_mesici: bool = False) -> None:
    """
    Každé třídní učitelce pošle JEDEN e-mail:
      - Pokud má třída problémy: seznam problémů + (při prvním spuštění v měsíci) připomínka kontroly
      - Pokud třída nemá problémy a je první spuštění v měsíci: jen připomínka kontroly
      - Jinak: nic
    """
    podle_tridy: dict = defaultdict(list)
    for z in zaznamy:
        podle_tridy[normalizuj_tridu(z["skupina"])].append(z)

    dnes = date.today()
    dnes_str = dnes.strftime("%d.%m.%Y")
    minuly_mesic = (dnes.replace(day=1) - timedelta(days=1))
    nazev_mesice = minuly_mesic.strftime("%B %Y")

    odeslano, bez_tridni, bez_emailu = 0, [], []

    for trida, tridni in sorted(tridni_ucitele.items()):
        seznam = podle_tridy.get(trida, [])
        ma_problemy = bool(seznam)

        # Nic k odeslání – třída OK a není první spuštění v měsíci
        if not ma_problemy and not prvni_v_mesici:
            continue

        email = najdi_email(tridni, email_dict)
        if not email:
            bez_emailu.append(f"{tridni} ({trida})")
            continue

        jmeno_kratke = prvni_jmeno(tridni)

        seznam_s = sorted(
            seznam,
            key=lambda z: (
                datum_pro_razeni(z["datum"]),
                int(z["hodina"]) if z["hodina"].isdigit() else 0
            )
        )

        # ── Hlavička e-mailu podle stavu ──────────────────────
        if ma_problemy:
            predmet = f"⚠️TK kontrola provedena – třída {trida} ({len(seznam_s)} probl.) – {dnes_str}"
            uvod_txt = (
                f"ve tvé třídě ({trida}) byly nalezeny"
                f" nezapsané nebo neúplné hodiny ({len(seznam_s)} záznamů).\n\n"
            )
            stav_html = f"""
<div class="warn-box">
  ⚠️ Třídní kniha třídy <b>{trida}</b> není v pořádku.<br>
  Celkem <b>{len(seznam_s)}</b> záznamů vyžaduje doplnění – příslušní vyučující byly kontaktováni.
</div>"""
        else:
            predmet = f"✅Zapiš do TK kontrola provedena {nazev_mesice} – třída {trida}."
            uvod_txt = (
                f"kontrola třídní knihy třídy {trida} za {nazev_mesice} proběhla v pořádku.\n"
                f"Všechny hodiny jsou správně zapsány – díky! ✅\n"
            )
            stav_html = f"""
<div class="ok-box">
  ✅ Třídní kniha třídy <b>{trida}</b> za <b>{nazev_mesice}</b> je v pořádku.<br>
  Všechny hodiny jsou správně zapsány – díky!
</div>"""

        # ── Připomínka kontroly (jen první spuštění v měsíci) ─
        pripominka_txt = (
            f"\nNezapomeň zapsat, že jsi kontrolovala třídní knihu v měsíci {nazev_mesice}.\n"
            f"\nProvedena kontrola třídní knihy za měsíc {nazev_mesice}.\n"
            if prvni_v_mesici else ""
        )
        pripominka_html = (
            f'<div class="reminder-box">📝 Nezapomeň zapsat, že jsi kontrolovala třídní knihu v měsíci <b>{nazev_mesice}.</b>'
            f'<br><b>Provedena kontrola třídní knihy za měsíc {nazev_mesice}.</b></div>'

            if prvni_v_mesici else ""
        )

        # ── Tabulka problémů (jen pokud existují) ─────────────
        if ma_problemy:
            podle_ucitele_tridni: dict = defaultdict(int)
            for z in seznam_s:
                podle_ucitele_tridni[z["vyucujici"]] += 1
            ucitele_tridni_html = "".join(
                f'<tr><td>{j}</td><td style="text-align:center;font-weight:bold;">{p}</td></tr>\n'
                for j, p in sorted(podle_ucitele_tridni.items(), key=lambda x: -x[1])
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

            radky_txt = "".join(
                f"  • {z['datum']} ({z['den']}) | {z['hodina']}. hod."
                f" | {z['predmet']} | {z['vyucujici']} | {z['poznamka']}\n"
                for z in seznam_s
            )

            tabulky_html = f"""
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
</table>"""
        else:
            radky_txt = ""
            tabulky_html = ""

        # ── Prostý text ────────────────────────────────────────
        zprava_txt = (
            f"Ahoj {jmeno_kratke},\n\n"
            f"{uvod_txt}"
            f"{radky_txt}"
            f"{pripominka_txt}"
            f"\nHezký den!\n"
        )

        # ── HTML ───────────────────────────────────────────────
        html = f"""<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8">
<style>
body{{font-family:Arial,sans-serif;font-size:14px;color:#222}}
h2{{font-size:17px;font-weight:500;margin:0 0 4px}}
h3{{font-size:14px;font-weight:500;margin:16px 0 6px;color:#2c3e50;
    border-bottom:2px solid #2c3e50;padding-bottom:3px}}
.sub{{font-size:13px;color:#666;margin:0 0 14px}}
.warn-box{{background:#fff3cd;border-left:4px solid #e6a817;padding:10px 14px;
           border-radius:4px;margin-bottom:14px;font-size:13px}}
.ok-box{{background:#d4edda;border-left:4px solid #28a745;padding:10px 14px;
         border-radius:4px;margin-bottom:14px;font-size:13px}}
.reminder-box{{background:#e8f4fd;border-left:4px solid #3498db;padding:10px 14px;
               border-radius:4px;margin:14px 0;font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#2c3e50;color:#fff;padding:7px 9px;text-align:left;font-weight:500}}
td{{padding:6px 9px;border-bottom:0.5px solid #ddd}}
.tbl-u{{max-width:380px}}
.tbl-u tr:nth-child(even) td{{background:#f5f5f5}}
.footer{{font-size:11px;color:#aaa;margin-top:14px;padding-top:10px;
         border-top:0.5px solid #e0e0e0}}
</style></head><body>
<h2>Třídní kniha – třída {trida} – {dnes_str}</h2>
<p class="sub">Ahoj {jmeno_kratke},</p>
{stav_html}
{pripominka_html}
{tabulky_html}
<p class="footer">Automaticky vygenerováno systémem Bakalátor 3000 – suplování &amp; třídnice. Při jakýchkoli potížích kontaktujte Radka.</p>
</body></html>"""

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
    dnes_dt = date.today() - timedelta(days=1)
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
    rozesli_emaily_tridnim(zaznamy, email_dict, tridni_ucitele,
                           prvni_v_mesici=je_prvni_spusteni_v_mesici())
    posli_souhrnny_report(zaznamy)
    reset_chyb()


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

    try:
        zkontroluj_tridnici()
    except Exception as e:
        chyba = f"⚠️  Nepodařilo se spustit program\n{e}"
        log.critical(chyba)
        posli_email(
            ADMIN_EMAIL,
            chyba,
            predmet="🚨 Kritické selhání – program zastaven"
        )
