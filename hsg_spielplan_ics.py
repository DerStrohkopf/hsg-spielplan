#!/usr/bin/env python3
"""
HSG Blomberg-Lippe: Spielplan von der Vereinsseite -> abonnierbarer iCal-Feed.

Aufruf:   python3 hsg_spielplan_ics.py --out /pfad/zum/webroot/hsg-blomberg-lippe.ics
Abhängig: pip install requests beautifulsoup4

- Jedes Spiel bekommt eine stabile UID (Wettbewerb + Heim + Gast). Ändert sich
  Datum oder Uhrzeit, wird der bestehende Kalendereintrag aktualisiert statt
  dupliziert. Verschwindet ein Spiel von der Seite, verschwindet es auch im Feed.
- Spiele mit "00:00 Uhr" (Uhrzeit noch nicht angesetzt) werden als
  ganztägiger Eintrag "(Uhrzeit offen)" geführt und später automatisch
  durch den Termin mit Uhrzeit ersetzt.
- Ausgabe auf stdout nur, wenn sich etwas geändert hat (ideal für Cron-Mail).
- Schutz: Werden zu wenige Spiele erkannt (Seitenumbau), bleibt der alte Feed
  unverändert und das Skript beendet sich mit Fehler.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = "https://hsg-blomberg-lippe.de/spielplan/"
TEAM = "HSG Blomberg-Lippe"
TZ = ZoneInfo("Europe/Berlin")
DURATION = timedelta(hours=2)   # angenommene Spieldauer inkl. Puffer
MIN_GAMES = 5                   # darunter gilt der Abruf als fehlgeschlagen
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})")


# --------------------------------------------------------------------------- #
# Abruf & Parsing
# --------------------------------------------------------------------------- #
def fetch_html() -> str:
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0 (spielplan-sync)"})
    r.raise_for_status()
    return r.text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _teams(cell):
    """Heim/Gast: bevorzugt aus den alt-Texten der Vereinslogos, sonst aus dem Text."""
    alts = [_clean(i.get("alt", "")) for i in cell.find_all("img") if _clean(i.get("alt", ""))]
    if len(alts) >= 2:
        return alts[0], alts[1]
    text = _clean(cell.get_text(" ", strip=True))
    if text.startswith(TEAM):
        return TEAM, _clean(text[len(TEAM):])
    if text.endswith(TEAM):
        return _clean(text[: -len(TEAM)]), TEAM
    raise ValueError(f"Begegnung nicht lesbar: {text!r}")


def parse_games(html: str):
    soup = BeautifulSoup(html, "html.parser")
    games, seen = [], set()
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        m = DATE_RE.search(_clean(cells[0].get_text(" ", strip=True)))
        if not m:
            continue
        d, mo, y, h, mi = map(int, m.groups())
        comp = _clean(cells[1].get_text(" ", strip=True))
        home, away = _teams(cells[2])

        score = re.findall(r"\d+", cells[3].get_text(" ", strip=True)) if len(cells) > 3 else []
        result = f"{score[0]}:{score[1]}" if len(score) >= 2 else ""

        allday = (h, mi) == (0, 0)
        if allday:
            start = date(y, mo, d)
        else:
            start = datetime(y, mo, d, h, mi, tzinfo=TZ)

        uid = hashlib.sha1(f"{comp}|{home}|{away}".encode()).hexdigest()[:20]
        if uid in seen:  # Sicherheitsnetz bei identischer Paarung im selben Wettbewerb
            uid = hashlib.sha1(f"{comp}|{home}|{away}|{start}".encode()).hexdigest()[:20]
        seen.add(uid)

        summary = f"{home} – {away}" + (" (Uhrzeit offen)" if allday else "")
        desc = f"{comp}"
        if result:
            desc += f"\nErgebnis: {result}"
        desc += f"\nQuelle: {URL}"
        games.append(
            dict(uid=f"{uid}@hsg-spielplan", start=start, allday=allday,
                 summary=summary, comp=comp, desc=desc, result=result)
        )
    return games


# --------------------------------------------------------------------------- #
# iCal
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    b, parts, limit = line.encode("utf-8"), [], 75
    while len(b) > limit:
        cut = limit
        while (b[cut] & 0xC0) == 0x80:  # nicht mitten in einem UTF-8-Zeichen trennen
            cut -= 1
        parts.append(b[:cut].decode("utf-8"))
        b, limit = b[cut:], 74
    parts.append(b.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(games) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Digitalschmiede//HSG Spielplan//DE",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:HSG Blomberg-Lippe Spielplan",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
    ]
    for g in games:
        lines += ["BEGIN:VEVENT", f"UID:{g['uid']}", f"DTSTAMP:{stamp}"]
        if g["allday"]:
            s = g["start"]
            lines += [f"DTSTART;VALUE=DATE:{s:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{s + timedelta(days=1):%Y%m%d}",
                      "STATUS:TENTATIVE"]
        else:
            s = g["start"].astimezone(timezone.utc)
            e = (g["start"] + DURATION).astimezone(timezone.utc)
            lines += [f"DTSTART:{s:%Y%m%dT%H%M%SZ}", f"DTEND:{e:%Y%m%dT%H%M%SZ}",
                      "STATUS:CONFIRMED"]
        lines += [f"SUMMARY:{_esc(g['summary'])}", f"DESCRIPTION:{_esc(g['desc'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


# --------------------------------------------------------------------------- #
# Änderungsvergleich
# --------------------------------------------------------------------------- #
def snapshot(games):
    return {g["uid"]: dict(summary=g["summary"], start=str(g["start"]), result=g["result"]) for g in games}


def diff(old, new):
    out = []
    for uid, n in new.items():
        if uid not in old:
            out.append(f"+ NEU        {n['start']}  {n['summary']}")
        elif old[uid]["start"] != n["start"] or old[uid]["summary"] != n["summary"]:
            out.append(f"~ GEÄNDERT  {old[uid]['start']} -> {n['start']}  {n['summary']}")
        elif old[uid].get("result") != n["result"] and n["result"]:
            out.append(f"= ERGEBNIS  {n['summary']}  {n['result']}")
    for uid, o in old.items():
        if uid not in new:
            out.append(f"- ENTFALLEN  {o['start']}  {o['summary']}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Zielpfad der .ics-Datei (im Webroot)")
    out = Path(ap.parse_args().out)
    snap_file = out.with_suffix(".json")

    games = parse_games(fetch_html())
    if len(games) < MIN_GAMES:
        sys.exit(f"FEHLER: nur {len(games)} Spiele erkannt – Seitenstruktur geändert? Feed bleibt unverändert.")
    games.sort(key=lambda g: (g["start"] if isinstance(g["start"], datetime)
                              else datetime.combine(g["start"], datetime.min.time(), TZ)))

    new = snapshot(games)
    old = json.loads(snap_file.read_text()) if snap_file.exists() else {}
    changes = diff(old, new)

    if changes or not out.exists():
        tmp = out.with_suffix(".tmp")
        tmp.write_text(build_ics(games), encoding="utf-8", newline="")
        tmp.replace(out)
        snap_file.write_text(json.dumps(new, ensure_ascii=False, indent=1))
        print(f"HSG-Spielplan aktualisiert ({len(games)} Spiele):")
        print("\n".join(changes) if old else "Erstmalig erzeugt.")


if __name__ == "__main__":
    main()
