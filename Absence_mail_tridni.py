"""
Odesílání měsíčního reportu absencí třídním učitelkám.
Spouštěno cronem 1x za měsíc.

Čte ze souborů:
  - absence_history.json  ... záznamy o odeslaných zprávách rodičům
  - tridni_ucitele.json   ... přiřazení třídy → jméno učitele
  - kontakty.json         ... přiřazení jméno → email

Cron (1x měsíčně, 1. den v měsíci v 7:00):
  0 7 1 * * /home/zvoneni/.venv/bin/python /home/zvoneni/bakalari/absence_report.py
"""

import json
import logging
import logging.handlers
import os
import smtplib
import sys
import traceback
from datetime import date, timedelta, datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

# ── Cesty ──────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
LOG_DIR        = BASE_DIR / "logs"
LOG_FILE       = LOG_DIR / "absence_report.log"
HISTORY_FILE   = BASE_DIR / "absence_history.json"
KONTAKTY_FILE  = BASE_DIR / "kontakty.json"
UCITELE_FILE   = BASE_DIR / "tridni_ucitele.json"
ENV_FILE       = BASE_DIR / ".env"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("absence_report")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(fmt="%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging()

# ── Konfigurace ────────────────────────────────────────────────────────────────
load_dotenv(ENV_FILE)

REPORT_DNI     = 31        # Za kolik dní zpět sestavit report
ODESILATEL     = os.getenv("ODESILATEL")
HESLO          = os.getenv("HESLO")
SMTP_SERVER    = "smtp.webzdarma.cz"
SMTP_PORT      = 465

# ── Načítání souborů ───────────────────────────────────────────────────────────
def load_json(soubor: Path, popis: str) -> object:
    if not soubor.exists():
        log.error(f"Soubor {popis} nenalezen: {soubor}")
        sys.exit(1)
    try:
        return json.loads(soubor.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Nepodařilo se načíst {popis} ({soubor}): {e}")
        sys.exit(1)


def load_kontakty(data: list) -> dict:
    """Vrátí slovník {jmeno: email}"""
    return {k["jmeno"]: k["email"] for k in data if "jmeno" in k and "email" in k}


def load_ucitele(data: dict) -> dict:
    """Vrátí slovník {trida: jmeno_ucitele}"""
    return data


def najdi_email(kontakty: dict, jmeno: str) -> str:
    """Najde email podle jména učitele. Toleruje mezery a diakritiku."""
    if jmeno in kontakty:
        return kontakty[jmeno]
    # Zkus case-insensitive shodu
    jmeno_lower = jmeno.lower().strip()
    for k, v in kontakty.items():
        if k.lower().strip() == jmeno_lower:
            return v
    return None


def trida_na_ucitele(trida: str, ucitele: dict) -> str:
    """Najde učitele pro třídu – zkouší přesnou shodu i zkrácený klíč (2.A → 2)."""
    if trida in ucitele:
        return ucitele[trida]
    zkraceny = trida.split(".")[0]
    if zkraceny in ucitele:
        return ucitele[zkraceny]
    # Zkus i s tečkou na konci (edge case "7.")
    if trida.rstrip(".") in ucitele:
        return ucitele[trida.rstrip(".")]
    return None

# ── Sestavení a odeslání emailu ────────────────────────────────────────────────
def sestavit_html(ucitel: str, zaznamy: list, datum_od: str, datum_do: str) -> str:
    """Sestaví HTML tělo reportu pro jednoho učitele."""

    # Seskup záznamy podle žáka
    podle_zaka = {}
    for z in zaznamy:
        klic = f"{z['trida']}|{z['zak']}"
        podle_zaka.setdefault(klic, []).append(z)

    radky_html = ""
    for i, (klic, zaznamy_zaka) in enumerate(sorted(podle_zaka.items())):
        z0       = zaznamy_zaka[0]
        trida    = z0["trida"]
        zak      = z0["zak"]
        barva_bg = "#ffffff" if i % 2 == 0 else "#f8f9fa"

        # Data kdy byly zprávy odeslány rodičům
        data_odeslani = "<br>".join(
            f"{date.fromisoformat(z['datum']).day}. {date.fromisoformat(z['datum']).month}. {date.fromisoformat(z['datum']).year} v {z['cas']}"
            for z in zaznamy_zaka
        )

        # Unikátní neomluvené dny přes všechny záznamy žáka
        seen = set()
        vsechny_dny = []
        for z in zaznamy_zaka:
            for d in z.get("dny", []):
                if d not in seen:
                    vsechny_dny.append(d)
                    seen.add(d)
        dny_str = "<br>".join(vsechny_dny) if vsechny_dny else "—"

        radky_html += f"""
      <tr style="background:{barva_bg};">
        <td style="padding:10px 14px;border-bottom:1px solid #e8ecf0;font-weight:bold;
                   white-space:nowrap;">{trida}</td>
        <td style="padding:10px 14px;border-bottom:1px solid #e8ecf0;">{zak}</td>
        <td style="padding:10px 14px;border-bottom:1px solid #e8ecf0;color:#1a5276;
                   font-size:13px;">{data_odeslani}</td>
        <td style="padding:10px 14px;border-bottom:1px solid #e8ecf0;color:#c0392b;
                   font-size:13px;">{dny_str}</td>
      </tr>"""

    pocet_upozorneni = len(zaznamy)
    pocet_zaku       = len(podle_zaka)

    return f"""<div style="font-family:Arial,sans-serif;color:#222;max-width:750px;">

  <div style="background:#1a5276;padding:18px 24px;border-radius:8px 8px 0 0;">
    <span style="color:#ffffff;font-size:16px;font-weight:bold;letter-spacing:0.3px;">
      ZŠ a MŠ Bohuslavice &nbsp;&#124;&nbsp; Měsíční report absencí
    </span>
  </div>

  <div style="background:#ffffff;padding:24px 24px 20px;border:1px solid #d5d8dc;
              border-top:none;border-radius:0 0 8px 8px;">

    <p style="margin-top:0;font-size:15px;">Dobrý den, {ucitel.split()[0] if ucitel else ""},</p>

    <p style="font-size:14px;line-height:1.6;">
      zasíláme Vám přehled žáků Vaší třídy, jejichž rodičům bylo v období
      <strong>{datum_od}&nbsp;–&nbsp;{datum_do}</strong>
      odesláno automatické upozornění na neomluvené absence.
    </p>

    <table style="border-collapse:collapse;width:100%;background:#fff;
                  border:1px solid #d5d8dc;border-radius:6px;margin-bottom:20px;">
      <thead>
        <tr style="background:#1a5276;color:#ffffff;">
          <th style="padding:11px 14px;text-align:left;font-weight:600;">Třída</th>
          <th style="padding:11px 14px;text-align:left;font-weight:600;">Žák / žákyně</th>
          <th style="padding:11px 14px;text-align:left;font-weight:600;">Zpráva rodičům odeslána</th>
          <th style="padding:11px 14px;text-align:left;font-weight:600;">Neomluvené dny</th>
        </tr>
      </thead>
      <tbody>
        {radky_html}
      </tbody>
    </table>

    <div style="background:#eaf4fb;border-left:4px solid #1a5276;padding:12px 16px;
                border-radius:0 6px 6px 0;margin-bottom:20px;font-size:13px;">
      Celkem upozornění rodičům: <strong>{pocet_upozorneni}</strong>
      &nbsp;&#124;&nbsp;
      Celkem žáků: <strong>{pocet_zaku}</strong>
    </div>

    <p style="font-size:14px;line-height:1.6;color:#555;">
      V případě dotazů kontaktujte Radka Matouška
      (<a href="mailto:matousek.radek@zsamsbohuslavice.cz"
          style="color:#1a5276;">matousek.radek@zsamsbohuslavice.cz</a>).
    </p>

    <p style="font-size:14px;margin-bottom:0;">
      S pozdravem,<br>
      <strong>ZŠ a MŠ Bohuslavice</strong>
    </p>

    <hr style="border:none;border-top:1px solid #e8ecf0;margin:20px 0 14px;">
    <p style="font-size:11px;color:#aaa;margin:0;">
      &#9881;&#65039; Tento report byl vygenerován automaticky systémem
      <em>Bakalátor 3000</em> – správa absencí.
    </p>
  </div>
</div>"""


def posli_email(email: str, predmet: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = predmet
    msg["From"]    = ODESILATEL
    msg["To"]      = email
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.login(ODESILATEL, HESLO)
        smtp.sendmail(ODESILATEL, email, msg.as_string())

# ── Hlavní logika ──────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 70)
    log.info(f"REPORT – spuštění, období posledních {REPORT_DNI} dní")
    log.info("=" * 70)

    # Kontrola konfigurace
    if not ODESILATEL or not HESLO:
        log.critical("ODESILATEL nebo HESLO není nastaven v .env. Ukončuji.")
        sys.exit(1)

    # Načti soubory
    history_data  = load_json(HISTORY_FILE, "absence_history.json")
    kontakty_data = load_json(KONTAKTY_FILE, "kontakty.json")
    ucitele_data  = load_json(UCITELE_FILE,  "tridni_ucitele.json")

    kontakty = load_kontakty(kontakty_data)
    ucitele  = load_ucitele(ucitele_data)

    log.info(f"Načteno: {len(history_data)} záznamů historie, "
             f"{len(kontakty)} kontaktů, {len(ucitele)} tříd")

    # Filtruj záznamy za posledních REPORT_DNI dní
    hranice    = date.today() - timedelta(days=REPORT_DNI)
    #datum_od   = hranice.strftime("%-d. %-m. %Y")
    datum_od = f"{hranice.day}. {hranice.month}. {hranice.year}"
    dnes = date.today()
    datum_do = f"{dnes.day}. {dnes.month}. {dnes.year}"


    relevantni = []
    for z in history_data:
        try:
            if date.fromisoformat(z["datum"]) >= hranice:
                relevantni.append(z)
        except Exception:
            continue

    if not relevantni:
        log.info(f"Žádné záznamy za posledních {REPORT_DNI} dní. Report se neodesílá.")
        sys.exit(0)

    log.info(f"Záznamy za období {datum_od} – {datum_do}: {len(relevantni)}")

    # Seskup záznamy podle třídy
    podle_tridy = {}
    for z in relevantni:
        trida = z.get("trida", "?")
        podle_tridy.setdefault(trida, []).append(z)

    # Odešli každé učitelce její report
    uspech   = 0
    preskoc  = 0
    chyby    = 0

    for trida, zaznamy in sorted(podle_tridy.items()):
        ucitel = trida_na_ucitele(trida, ucitele)
        if not ucitel:
            log.warning(f"Třída '{trida}': učitel nenalezen v tridni_ucitele.json. Přeskakuji.")
            preskoc += 1
            continue

        email = najdi_email(kontakty, ucitel)
        if not email:
            log.warning(f"Třída '{trida}', učitel '{ucitel}': email nenalezen v kontakty.json. Přeskakuji.")
            preskoc += 1
            continue

        log.info(f"Odesílám report: třída {trida} → {ucitel} ({email}), "
                 f"{len(zaznamy)} záznamů")
        try:
            html    = sestavit_html(ucitel, zaznamy, datum_od, datum_do)
            predmet = f"Report absencí {datum_od} – {datum_do} | třída {trida}"
            posli_email(email, predmet, html)
            log.info(f"  ✓ Odesláno na {email}")
            uspech += 1
        except Exception as e:
            log.error(f"  ✗ Nepodařilo se odeslat na {email}: {e}\n{traceback.format_exc()}")
            chyby += 1

    log.info(f"Výsledek: odesláno {uspech}, přeskočeno {preskoc}, chyb {chyby}.")
    log.info("=" * 70)
    sys.exit(1 if chyby else 0)


if __name__ == "__main__":
    main()