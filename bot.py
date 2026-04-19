import os, json, base64
import pytz
from datetime import time as dtime, datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from groq import Groq
from anthropic import Anthropic
from notion_client import Client as NotionClient

load_dotenv()

groq   = Groq(api_key=os.environ["GROQ_API_KEY"])
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
notion = NotionClient(auth=os.environ["NOTION_TOKEN"])
DB     = os.environ["NOTION_DATABASE_ID"]

ZEITZONE      = pytz.timezone("Europe/Berlin")
CHAT_IDS_FILE = "/tmp/chat_ids.txt"
KATEGORIEN    = ["Marketing", "Finanzen", "Operations", "Produkt", "Sonstiges"]
chat_ids      = set()
letzte_aktion = {}

HAUPTMENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/liste"), KeyboardButton("/heute")],
        [KeyboardButton("/briefing"), KeyboardButton("/erledigt")],
        [KeyboardButton("/ideen"), KeyboardButton("/fokus")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def undo_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Rückgängig", callback_data="rueckgaengig")]
    ])


# ── Chat-ID ──────────────────────────────────────────────────

def lade_chat_ids():
    if os.path.exists(CHAT_IDS_FILE):
        with open(CHAT_IDS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    chat_ids.add(int(line))

def speichere_chat_id(chat_id: int):
    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        with open(CHAT_IDS_FILE, "a") as f:
            f.write(f"{chat_id}\n")


# ── Kernfunktionen ───────────────────────────────────────────

def transkribiere(path: str) -> str:
    with open(path, "rb") as f:
        return groq.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="de"
        ).text

def analysiere(text: str, offene: list[dict]) -> dict:
    heute          = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    offene_str     = "\n".join(f"- {t['titel']}" for t in offene) if offene else "(keine)"
    kategorien_str = ", ".join(KATEGORIEN)
    r = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""Analysiere diese Sprachnachricht und antworte NUR mit JSON.

Heute ist: {heute}
Sprachnachricht: "{text}"

Offene To-dos:
{offene_str}

STRIKTE REGEL für "notiz_zu_todo": NUR wenn der Nutzer explizit sagt: "notiz", "note", "füg hinzu", "hinzufügen", "ergänze", "ergänzen".

Wähle eines dieser Formate:

Neue Aufgabe(n):
{{"aktion": "neu_todo", "todos": [{{"titel": "Aufgabe", "prioritaet": "wichtig", "faelligkeit": "2024-01-15", "kategorie": "Marketing"}}]}}
- prioritaet: "wichtig" oder "neutral"
- faelligkeit: ISO-Datum NUR wenn Zeitraum/Datum genannt, sonst null
- kategorie: eine aus [{kategorien_str}] wenn erkennbar, sonst null

Neue Notiz:
{{"aktion": "neu_notiz", "titel": "Titel", "inhalt": "Text"}}

Neue Idee (wenn "Idee", "was wenn", "ich überlege", "Gedanke" gesagt wird):
{{"aktion": "neu_idee", "titel": "Titel", "inhalt": "Beschreibung"}}

Notiz zu bestehendem To-do (NUR mit Schlüsselwort notiz/füg hinzu/ergänze):
{{"aktion": "notiz_zu_todo", "titel": "Titel aus Liste", "notiz": "Text"}}

Aufgabe erledigt:
{{"aktion": "erledigt", "titel": "Titel aus Liste"}}

Aufgabe verschieben (wenn "verschiebe", "verlege", "auf [Datum]" gesagt wird):
{{"aktion": "verschieben", "titel": "Titel aus Liste", "faelligkeit": "2024-01-20"}}

Priorität ändern (wenn "markiere als wichtig/neutral", "ist wichtig/unwichtig" gesagt wird):
{{"aktion": "prioritaet_aendern", "titel": "Titel aus Liste", "prioritaet": "wichtig"}}

Wochenfokus setzen (wenn "fokus", "Schwerpunkt", "Wochenziel" gesagt wird):
{{"aktion": "fokus_setzen", "titel": "Titel aus Liste oder neue Aufgabe"}}

Nur JSON, kein anderer Text."""}]
    )
    antwort = r.content[0].text.strip()
    if "```" in antwort:
        antwort = antwort.split("```")[1]
        if antwort.startswith("json"):
            antwort = antwort[4:]
    return json.loads(antwort.strip())

def datum_anzeige(iso_datum: str | None) -> str:
    if not iso_datum:
        return ""
    try:
        d     = datetime.strptime(iso_datum, "%Y-%m-%d").date()
        heute = datetime.now(ZEITZONE).date()
        delta = (d - heute).days
        if delta < 0:
            return f"⚠️ überfällig ({abs(delta)}d)"
        elif delta == 0:
            return "📅 heute"
        elif delta == 1:
            return "📅 morgen"
        elif delta == 2:
            return "📅 übermorgen"
        elif delta <= 7:
            return f"📅 in {delta} Tagen"
        else:
            return f"📅 {d.strftime('%d.%m.%Y')}"
    except Exception:
        return ""

def todo_hinzufügen(titel: str, prioritaet: str = "neutral",
                    faelligkeit: str = None, kategorie: str = None) -> str:
    props = {
        "Name":      {"title":    [{"text": {"content": titel}}]},
        "Erledigt":  {"checkbox": False},
        "Priorität": {"select":   {"name": "Wichtig" if prioritaet == "wichtig" else "Neutral"}},
        "Typ":       {"select":   {"name": "To-do"}},
    }
    if faelligkeit:
        props["Fälligkeit"] = {"date": {"start": faelligkeit}}
    if kategorie and kategorie in KATEGORIEN:
        props["Kategorie"] = {"select": {"name": kategorie}}
    return notion.pages.create(parent={"database_id": DB}, properties=props)["id"]

def notiz_hinzufügen(titel: str, inhalt: str) -> str:
    return notion.pages.create(
        parent={"database_id": DB},
        properties={
            "Name":    {"title":     [{"text": {"content": titel}}]},
            "Typ":     {"select":    {"name": "Notiz"}},
            "Notizen": {"rich_text": [{"text": {"content": inhalt}}]},
        }
    )["id"]

def idee_hinzufügen(titel: str, inhalt: str) -> str:
    return notion.pages.create(
        parent={"database_id": DB},
        properties={
            "Name":    {"title":     [{"text": {"content": titel}}]},
            "Typ":     {"select":    {"name": "Idee"}},
            "Notizen": {"rich_text": [{"text": {"content": inhalt}}]},
        }
    )["id"]

def notiz_zu_todo_hinzufügen(page_id: str, neue_notiz: str) -> str:
    seite      = notion.pages.retrieve(page_id=page_id)
    alte_notiz = ""
    try:
        alte_notiz = seite["properties"]["Notizen"]["rich_text"][0]["plain_text"]
    except (KeyError, IndexError):
        pass
    kombiniert = (alte_notiz + "\n" + neue_notiz).strip() if alte_notiz else neue_notiz
    notion.pages.update(
        page_id=page_id,
        properties={"Notizen": {"rich_text": [{"text": {"content": kombiniert}}]}}
    )
    return alte_notiz

def offene_todos() -> list[dict]:
    seiten = notion.databases.query(
        database_id=DB,
        filter={"and": [
            {"property": "Erledigt", "checkbox": {"equals": False}},
            {"property": "Typ",      "select":   {"equals": "To-do"}},
        ]}
    )["results"]
    todos = []
    for s in seiten:
        if not s["properties"]["Name"]["title"]:
            continue
        prio_prop  = s["properties"].get("Priorität") or {}
        prio_sel   = prio_prop.get("select") or {}
        fäll_prop  = s["properties"].get("Fälligkeit") or {}
        fäll_date  = fäll_prop.get("date") or {}
        kat_prop   = s["properties"].get("Kategorie") or {}
        kat_sel    = kat_prop.get("select") or {}
        fokus_prop = s["properties"].get("Fokus") or {}
        todos.append({
            "id":          s["id"],
            "titel":       s["properties"]["Name"]["title"][0]["plain_text"],
            "prioritaet":  prio_sel.get("name", "Neutral"),
            "faelligkeit": fäll_date.get("start"),
            "kategorie":   kat_sel.get("name"),
            "fokus":       fokus_prop.get("checkbox", False),
        })
    heute = datetime.now(ZEITZONE).date()
    def sort_key(t):
        if t.get("fokus"):
            return (-1, 0)
        if t["faelligkeit"]:
            try:
                d     = datetime.strptime(t["faelligkeit"], "%Y-%m-%d").date()
                delta = (d - heute).days
                return (0 if delta <= 0 else 1, delta)
            except Exception:
                pass
        return (2, 0 if t["prioritaet"] == "Wichtig" else 1)
    todos.sort(key=sort_key)
    return todos

def alle_ideen() -> list[dict]:
    seiten = notion.databases.query(
        database_id=DB,
        filter={"property": "Typ", "select": {"equals": "Idee"}}
    )["results"]
    ideen = []
    for s in seiten:
        if not s["properties"]["Name"]["title"]:
            continue
        notizen_prop = s["properties"].get("Notizen") or {}
        rich_text    = notizen_prop.get("rich_text") or []
        ideen.append({
            "id":     s["id"],
            "titel":  s["properties"]["Name"]["title"][0]["plain_text"],
            "inhalt": rich_text[0]["plain_text"] if rich_text else "",
        })
    return ideen

def aktueller_fokus() -> dict | None:
    seiten = notion.databases.query(
        database_id=DB,
        filter={"and": [
            {"property": "Fokus",    "checkbox": {"equals": True}},
            {"property": "Erledigt", "checkbox": {"equals": False}},
        ]}
    )["results"]
    if not seiten:
        return None
    s = seiten[0]
    if not s["properties"]["Name"]["title"]:
        return None
    return {"id": s["id"], "titel": s["properties"]["Name"]["title"][0]["plain_text"]}

def alle_fokus_loeschen():
    seiten = notion.databases.query(
        database_id=DB,
        filter={"property": "Fokus", "checkbox": {"equals": True}}
    )["results"]
    for s in seiten:
        notion.pages.update(page_id=s["id"], properties={"Fokus": {"checkbox": False}})

def fokus_setzen_by_id(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Fokus": {"checkbox": True}})

def als_erledigt_markieren(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Erledigt": {"checkbox": True}})

def als_offen_markieren(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Erledigt": {"checkbox": False}})

def archivieren(page_id: str):
    notion.pages.update(page_id=page_id, archived=True)

def todos_als_liste_text(todos: list[dict], nummeriert: bool = True) -> str:
    zeilen = []
    for i, t in enumerate(todos, 1):
        if t.get("fokus"):
            emoji = "🎯"
        elif t["prioritaet"] == "Wichtig":
            emoji = "🔴"
        else:
            emoji = "⚪"
        prefix = f"{i}. " if nummeriert else "• "
        datum  = datum_anzeige(t.get("faelligkeit"))
        kat    = f"[{t['kategorie']}]" if t.get("kategorie") else ""
        extras = " ".join(filter(None, [datum, kat]))
        zeile  = f"{prefix}{emoji} {t['titel']}"
        if extras:
            zeile += f"  {extras}"
        zeilen.append(zeile)
    return "\n".join(zeilen)

async def ki_top3(todos: list[dict]) -> str:
    if not todos:
        return ""
    heute     = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    todos_str = "\n".join(
        f"- {t['titel']} | Priorität: {t['prioritaet']} | Fällig: {t.get('faelligkeit') or 'offen'} | Fokus: {'ja' if t.get('fokus') else 'nein'}"
        for t in todos
    )
    r = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""Du bist ein produktiver eCom Business-Assistent. Heute ist {heute}.
Wähle die Top 3 To-dos für heute. Falls ein Fokus-To-do existiert, nimm es immer auf. Antworte auf Deutsch, direkt und motivierend.

To-dos:
{todos_str}

Format:
🎯 Deine Top 3 für heute:
1. [Titel] – [kurze Begründung]
2. [Titel] – [kurze Begründung]
3. [Titel] – [kurze Begründung]"""}]
    )
    return r.content[0].text.strip()

async def foto_analysieren(path: str, caption: str = "") -> str:
    with open(path, "rb") as f:
        img_data = base64.standard_b64encode(f.read()).decode("utf-8")
    r = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_data}},
            {"type": "text",  "text": f"Beschreibe dieses Bild kurz und prägnant auf Deutsch. Was ist darauf zu sehen? Welche wichtigen Zahlen, Texte oder Informationen sind erkennbar?{' Kontext: ' + caption if caption else ''}"}
        ]}]
    )
    return r.content[0].text.strip()


# ── Telegram Handler ─────────────────────────────────────────

async def sprachnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global letzte_aktion
    speichere_chat_id(update.effective_chat.id)
    msg   = await update.message.reply_text("Verarbeite...", reply_markup=HAUPTMENU)
    voice = update.message.voice or update.message.audio
    file  = await ctx.bot.get_file(voice.file_id)
    path  = f"/tmp/{voice.file_id}.ogg"
    await file.download_to_drive(path)
    try:
        text   = transkribiere(path)
        offene = offene_todos()
        result = analysiere(text, offene)

        if result["aktion"] == "neu_todo":
            todos = result.get("todos", [])
            if isinstance(todos, dict):
                todos = [todos]
            if not todos:
                await msg.edit_text(f"Transkription:\n{text}\n\nKeine To-dos erkannt.")
                return
            page_ids = []
            for todo in todos:
                pid = todo_hinzufügen(
                    todo["titel"], todo.get("prioritaet", "neutral"),
                    todo.get("faelligkeit"), todo.get("kategorie"),
                )
                page_ids.append(pid)
            letzte_aktion = {"typ": "neu_todo", "page_ids": page_ids, "titel": [t["titel"] for t in todos]}
            zeilen = []
            for todo in todos:
                emoji  = "🔴" if todo.get("prioritaet") == "wichtig" else "⚪"
                extras = []
                if todo.get("faelligkeit"):
                    extras.append(datum_anzeige(todo["faelligkeit"]))
                if todo.get("kategorie"):
                    extras.append(f"[{todo['kategorie']}]")
                zeile = f"{emoji} {todo['titel']}"
                if extras:
                    zeile += "  " + " ".join(extras)
                zeilen.append(zeile)
            await msg.edit_text(
                f"✅ {len(todos)} To-do(s) hinzugefügt:\n\n" + "\n".join(zeilen),
                reply_markup=undo_button()
            )

        elif result["aktion"] == "neu_notiz":
            pid = notiz_hinzufügen(result["titel"], result["inhalt"])
            letzte_aktion = {"typ": "neu_notiz", "page_id": pid, "titel": result["titel"]}
            await msg.edit_text(
                f"📝 Notiz gespeichert:\n\n{result['inhalt']}",
                reply_markup=undo_button()
            )

        elif result["aktion"] == "neu_idee":
            pid = idee_hinzufügen(result["titel"], result["inhalt"])
            letzte_aktion = {"typ": "neu_notiz", "page_id": pid, "titel": result["titel"]}
            await msg.edit_text(
                f"💡 Idee gespeichert:\n\n{result['titel']}\n{result['inhalt']}",
                reply_markup=undo_button()
            )

        elif result["aktion"] == "notiz_zu_todo":
            gesuchter_titel = result.get("titel", "")
            notiz_text      = result.get("notiz", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                alte_notiz = notiz_zu_todo_hinzufügen(treffer["id"], notiz_text)
                letzte_aktion = {
                    "typ": "notiz_zu_todo", "page_id": treffer["id"],
                    "alte_notiz": alte_notiz, "titel": treffer["titel"],
                }
                await msg.edit_text(
                    f"📝 Notiz zu '{treffer['titel']}' ergänzt:\n\n{notiz_text}",
                    reply_markup=undo_button()
                )
            else:
                await msg.edit_text("Kein passendes To-do gefunden. Nutze /liste.")

        elif result["aktion"] == "erledigt":
            gesuchter_titel = result.get("titel", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                als_erledigt_markieren(treffer["id"])
                letzte_aktion = {"typ": "erledigt", "page_id": treffer["id"], "titel": treffer["titel"]}
                await msg.edit_text(f"✅ Erledigt: {treffer['titel']}", reply_markup=undo_button())
            else:
                await msg.edit_text(f"Transkription:\n{text}\n\nKein passendes To-do gefunden. Nutze /erledigt.")

        elif result["aktion"] == "verschieben":
            gesuchter_titel  = result.get("titel", "")
            neue_faelligkeit = result.get("faelligkeit")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer and neue_faelligkeit:
                letzte_aktion = {
                    "typ": "verschieben", "page_id": treffer["id"],
                    "alte_faelligkeit": treffer.get("faelligkeit"), "titel": treffer["titel"],
                }
                notion.pages.update(
                    page_id=treffer["id"],
                    properties={"Fälligkeit": {"date": {"start": neue_faelligkeit}}}
                )
                await msg.edit_text(
                    f"📅 '{treffer['titel']}' verschoben auf {datum_anzeige(neue_faelligkeit)}",
                    reply_markup=undo_button()
                )
            else:
                await msg.edit_text("Kein passendes To-do gefunden oder kein Datum erkannt.")

        elif result["aktion"] == "prioritaet_aendern":
            gesuchter_titel = result.get("titel", "")
            neue_prioritaet = result.get("prioritaet", "neutral")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                letzte_aktion = {
                    "typ": "prioritaet_aendern", "page_id": treffer["id"],
                    "alte_prioritaet": treffer["prioritaet"], "titel": treffer["titel"],
                }
                notion.pages.update(
                    page_id=treffer["id"],
                    properties={"Priorität": {"select": {"name": "Wichtig" if neue_prioritaet == "wichtig" else "Neutral"}}}
                )
                emoji = "🔴" if neue_prioritaet == "wichtig" else "⚪"
                await msg.edit_text(
                    f"{emoji} '{treffer['titel']}' → {'Wichtig' if neue_prioritaet == 'wichtig' else 'Neutral'}",
                    reply_markup=undo_button()
                )
            else:
                await msg.edit_text("Kein passendes To-do gefunden.")

        elif result["aktion"] == "fokus_setzen":
            gesuchter_titel = result.get("titel", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                alle_fokus_loeschen()
                fokus_setzen_by_id(treffer["id"])
                letzte_aktion = {"typ": "fokus_setzen", "page_id": treffer["id"], "titel": treffer["titel"]}
                await msg.edit_text(f"🎯 Wochenfokus gesetzt: {treffer['titel']}", reply_markup=undo_button())
            else:
                pid = todo_hinzufügen(gesuchter_titel, "wichtig")
                alle_fokus_loeschen()
                fokus_setzen_by_id(pid)
                letzte_aktion = {"typ": "fokus_setzen_neu", "page_id": pid, "titel": gesuchter_titel}
                await msg.edit_text(f"🎯 Wochenfokus (neue Aufgabe): {gesuchter_titel}", reply_markup=undo_button())

    except Exception as e:
        await msg.edit_text(f"Fehler: {e}")
    finally:
        os.remove(path)

async def foto_nachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global letzte_aktion
    speichere_chat_id(update.effective_chat.id)
    msg   = await update.message.reply_text("📸 Analysiere Screenshot...", reply_markup=HAUPTMENU)
    photo = update.message.photo[-1]
    file  = await ctx.bot.get_file(photo.file_id)
    path  = f"/tmp/{photo.file_id}.jpg"
    await file.download_to_drive(path)
    try:
        caption      = update.message.caption or ""
        beschreibung = await foto_analysieren(path, caption)
        titel        = caption[:50] if caption else f"Screenshot {datetime.now(ZEITZONE).strftime('%d.%m. %H:%M')}"
        pid          = notiz_hinzufügen(titel, beschreibung)
        letzte_aktion = {"typ": "neu_notiz", "page_id": pid, "titel": titel}
        await msg.edit_text(
            f"📸 Screenshot als Notiz gespeichert:\n\n{beschreibung}",
            reply_markup=undo_button()
        )
    except Exception as e:
        await msg.edit_text(f"Fehler: {e}")
    finally:
        os.remove(path)

async def callback_rueckgaengig(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global letzte_aktion
    query = update.callback_query
    await query.answer()
    if not letzte_aktion:
        await query.edit_message_text("Keine Aktion zum Rückgängigmachen vorhanden.")
        return
    try:
        typ = letzte_aktion["typ"]
        if typ == "neu_todo":
            for pid in letzte_aktion["page_ids"]:
                archivieren(pid)
            await query.edit_message_text(f"↩️ To-do(s) gelöscht: {', '.join(letzte_aktion['titel'])}")
        elif typ == "neu_notiz":
            archivieren(letzte_aktion["page_id"])
            await query.edit_message_text(f"↩️ Gelöscht: {letzte_aktion['titel']}")
        elif typ == "notiz_zu_todo":
            alte = letzte_aktion["alte_notiz"]
            notion.pages.update(
                page_id=letzte_aktion["page_id"],
                properties={"Notizen": {"rich_text": [{"text": {"content": alte}}] if alte else []}}
            )
            await query.edit_message_text(f"↩️ Notiz bei '{letzte_aktion['titel']}' zurückgesetzt.")
        elif typ == "erledigt":
            als_offen_markieren(letzte_aktion["page_id"])
            await query.edit_message_text(f"↩️ '{letzte_aktion['titel']}' wieder geöffnet.")
        elif typ == "verschieben":
            alte = letzte_aktion.get("alte_faelligkeit")
            notion.pages.update(
                page_id=letzte_aktion["page_id"],
                properties={"Fälligkeit": {"date": {"start": alte} if alte else None}}
            )
            await query.edit_message_text(f"↩️ Fälligkeit von '{letzte_aktion['titel']}' zurückgesetzt.")
        elif typ == "prioritaet_aendern":
            notion.pages.update(
                page_id=letzte_aktion["page_id"],
                properties={"Priorität": {"select": {"name": letzte_aktion["alte_prioritaet"]}}}
            )
            await query.edit_message_text(f"↩️ Priorität von '{letzte_aktion['titel']}' zurückgesetzt.")
        elif typ == "fokus_setzen":
            notion.pages.update(page_id=letzte_aktion["page_id"], properties={"Fokus": {"checkbox": False}})
            await query.edit_message_text("↩️ Wochenfokus zurückgesetzt.")
        elif typ == "fokus_setzen_neu":
            archivieren(letzte_aktion["page_id"])
            await query.edit_message_text("↩️ Fokus-Aufgabe gelöscht.")
        letzte_aktion = {}
    except Exception as e:
        await query.edit_message_text(f"Fehler: {e}")

async def textnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    text  = update.message.text.strip().lower()
    todos = offene_todos()

    # Fokus-Auswahl per Nummer
    if text.isdigit() and ctx.user_data.get("warte_auf_fokus"):
        n           = int(text)
        gespeichert = ctx.user_data.get("todo_liste", todos)
        if 1 <= n <= len(gespeichert):
            todo = gespeichert[n - 1]
            alle_fokus_loeschen()
            fokus_setzen_by_id(todo["id"])
            ctx.user_data.pop("warte_auf_fokus", None)
            ctx.user_data.pop("todo_liste", None)
            await update.message.reply_text(f"🎯 Wochenfokus gesetzt: {todo['titel']}", reply_markup=HAUPTMENU)
        else:
            await update.message.reply_text(f"Nummer {n} existiert nicht.", reply_markup=HAUPTMENU)
        return

    # Erledigt
    if text.startswith("erledigt"):
        teile = text.split()
        if len(teile) == 2 and teile[1].isdigit():
            n = int(teile[1])
            if 1 <= n <= len(todos):
                todo = todos[n - 1]
                als_erledigt_markieren(todo["id"])
                letzte_aktion.update({"typ": "erledigt", "page_id": todo["id"], "titel": todo["titel"]})
                await update.message.reply_text(f"✅ Erledigt: {todo['titel']}", reply_markup=undo_button())
            else:
                await update.message.reply_text(
                    f"Nummer {n} existiert nicht. Du hast {len(todos)} offene To-dos.", reply_markup=HAUPTMENU
                )
            return
        if not todos:
            await update.message.reply_text("Keine offenen To-dos! 🎉", reply_markup=HAUPTMENU)
            return
        ctx.user_data["todo_liste"] = todos
        await update.message.reply_text(
            f"Welche Aufgabe ist erledigt? Antworte mit der Nummer:\n\n{todos_als_liste_text(todos)}",
            reply_markup=HAUPTMENU
        )
        return

    # Allgemeine Zahl
    if text.isdigit():
        n           = int(text)
        gespeichert = ctx.user_data.get("todo_liste", todos)
        if 1 <= n <= len(gespeichert):
            todo = gespeichert[n - 1]
            als_erledigt_markieren(todo["id"])
            letzte_aktion.update({"typ": "erledigt", "page_id": todo["id"], "titel": todo["titel"]})
            ctx.user_data.pop("todo_liste", None)
            await update.message.reply_text(f"✅ Erledigt: {todo['titel']}", reply_markup=undo_button())
        else:
            await update.message.reply_text(
                "Nummer nicht gefunden. Tippe 'erledigt' für die aktuelle Liste.", reply_markup=HAUPTMENU
            )
        return

    await update.message.reply_text(
        "Schick mir eine Sprachnachricht oder ein Foto.\nNutze die Buttons unten für schnellen Zugriff.",
        reply_markup=HAUPTMENU
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "👋 Hey! Ich bin dein To-do Bot.\n\n"
        "📱 Sprachbefehle:\n"
        "• Aufgabe ansagen → wird als To-do gespeichert\n"
        "• 'Verschiebe [X] auf morgen' → Datum ändern\n"
        "• 'Markiere [X] als wichtig' → Priorität ändern\n"
        "• 'Fokus: [X]' → Wochenfokus setzen\n"
        "• 'Idee: [X]' → Idee speichern\n\n"
        "📸 Foto schicken → wird als Notiz mit KI-Beschreibung gespeichert\n\n"
        "Buttons unten für schnellen Zugriff.",
        reply_markup=HAUPTMENU
    )

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉", reply_markup=HAUPTMENU)
        return
    await update.message.reply_text(f"📋 Offene To-dos:\n\n{todos_als_liste_text(todos)}", reply_markup=HAUPTMENU)

async def cmd_heute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos     = offene_todos()
    heute_str = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    relevant  = [
        t for t in todos
        if t.get("fokus")
        or (t.get("faelligkeit") and t["faelligkeit"] <= heute_str)
        or (not t.get("faelligkeit") and t["prioritaet"] == "Wichtig")
    ]
    if not relevant:
        await update.message.reply_text("Heute nichts Dringendes! 🎉\nNutze /liste für alle.", reply_markup=HAUPTMENU)
        return
    await update.message.reply_text(f"📅 Heute relevant:\n\n{todos_als_liste_text(relevant)}", reply_markup=HAUPTMENU)

async def cmd_erledigt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉", reply_markup=HAUPTMENU)
        return
    ctx.user_data["todo_liste"] = todos
    await update.message.reply_text(
        f"Welche Aufgabe ist erledigt? Antworte mit der Nummer:\n\n{todos_als_liste_text(todos)}",
        reply_markup=HAUPTMENU
    )

async def cmd_briefing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    msg   = await update.message.reply_text("Analysiere deine To-dos...", reply_markup=HAUPTMENU)
    todos = offene_todos()
    if not todos:
        await msg.edit_text("Keine offenen To-dos – du bist up to date! 🎉")
        return
    await msg.edit_text(await ki_top3(todos))

async def cmd_ideen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    ideen = alle_ideen()
    if not ideen:
        await update.message.reply_text(
            "Noch keine Ideen! 💡\nSag einfach 'Idee: ...' in einer Sprachnachricht.", reply_markup=HAUPTMENU
        )
        return
    zeilen = []
    for idee in ideen:
                zeile = f"💡 {idee['titel']}"
        if idee["inhalt"]:
            preview = idee["inhalt"][:80] + ("..." if len(idee["inhalt"]) > 80 else "")
            zeile += f"\n   {preview}"
        zeilen.append(zeile)
    await update.message.reply_text(
        f"💡 Ideen-Sammlung ({len(ideen)}):\n\n" + "\n\n".join(zeilen), reply_markup=HAUPTMENU
    )

async def cmd_fokus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    fokus = aktueller_fokus()
    todos = offene_todos()
    teile = []
    if fokus:
        teile.append(f"🎯 Aktueller Wochenfokus:\n{fokus['titel']}\n")
    if not todos:
        await update.message.reply_text((teile[0] if teile else "") + "Keine offenen To-dos.", reply_markup=HAUPTMENU)
        return
    teile.append("Welche Aufgabe soll dein Wochenfokus sein?\nAntworte mit der Nummer:\n")
    teile.append(todos_als_liste_text(todos))
    ctx.user_data["warte_auf_fokus"] = True
    ctx.user_data["todo_liste"]      = todos
    await update.message.reply_text("\n".join(teile), reply_markup=HAUPTMENU)

async def cmd_suche(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    if not ctx.args:
        await update.message.reply_text("Nutze: /suche [Begriff]\nBeispiel: /suche Lieferant", reply_markup=HAUPTMENU)
        return
    suchbegriff = " ".join(ctx.args)
    ergebnisse  = notion.databases.query(
        database_id=DB,
        filter={"property": "Name", "title": {"contains": suchbegriff}}
    )["results"]
    if not ergebnisse:
        await update.message.reply_text(f"Nichts gefunden für '{suchbegriff}'.", reply_markup=HAUPTMENU)
        return
    zeilen = []
    for s in ergebnisse:
        if not s["properties"]["Name"]["title"]:
            continue
        titel    = s["properties"]["Name"]["title"][0]["plain_text"]
        typ_sel  = (s["properties"].get("Typ") or {}).get("select") or {}
        typ      = typ_sel.get("name", "?")
        erledigt = s["properties"].get("Erledigt", {}).get("checkbox", False)
        status   = "✅" if erledigt else ("💡" if typ == "Idee" else ("📝" if typ == "Notiz" else "⬜"))
        zeilen.append(f"{status} [{typ}] {titel}")
    await update.message.reply_text(
        f"🔍 '{suchbegriff}' – {len(zeilen)} Ergebnis(se):\n\n" + "\n".join(zeilen), reply_markup=HAUPTMENU
    )

async def morgen_erinnerung(ctx: ContextTypes.DEFAULT_TYPE):
    todos        = offene_todos()
    heute_str    = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    ist_montag   = datetime.now(ZEITZONE).weekday() == 0
    überfällig   = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] < heute_str]
    heute_fällig = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] == heute_str]
    fokus        = aktueller_fokus()
    teile        = [f"☀️ Guten Morgen! {len(todos)} offene To-dos."]
    if fokus:
        teile.append(f"\n🎯 Wochenfokus: {fokus['titel']}")
    elif ist_montag:
        teile.append("\n🎯 Es ist Montag! Setze deinen Wochenfokus mit /fokus oder per Sprache.")
    if überfällig:
        teile.append(f"\n⚠️ Überfällig ({len(überfällig)}):")
        teile.append(todos_als_liste_text(überfällig, nummeriert=False))
    if heute_fällig:
        teile.append(f"\n📅 Heute fällig ({len(heute_fällig)}):")
        teile.append(todos_als_liste_text(heute_fällig, nummeriert=False))
    top3 = await ki_top3(todos)
    if top3:
        teile.append(f"\n{top3}")
    text = "\n".join(teile)
    for cid in chat_ids:
        await ctx.bot.send_message(chat_id=cid, text=text)

async def abend_zusammenfassung(ctx: ContextTypes.DEFAULT_TYPE):
    todos      = offene_todos()
    heute_str  = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    überfällig = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] <= heute_str]
    if not todos:
        text = "🌙 Guten Abend! Alle To-dos erledigt – super gemacht! 🎉"
    else:
        teile = [f"🌙 Guten Abend! Noch {len(todos)} offene To-dos."]
        if überfällig:
            teile.append("\n⚠️ Überfällig – morgen angehen:")
            teile.append(todos_als_liste_text(überfällig, nummeriert=False))
        teile.append("\n📋 Alle offenen To-dos:")
        teile.append(todos_als_liste_text(todos))
        text = "\n".join(teile)
    for cid in chat_ids:
        await ctx.bot.send_message(chat_id=cid, text=text)

async def wochen_review(ctx: ContextTypes.DEFAULT_TYPE):
    if datetime.now(ZEITZONE).weekday() != 6:
        return
    todos      = offene_todos()
    heute_str  = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    überfällig = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] < heute_str]
    wichtige   = [t for t in todos if t["prioritaet"] == "Wichtig"]
    teile      = ["📊 Wöchentlicher Review\n", f"📋 Offene To-dos gesamt: {len(todos)}"]
    if überfällig:
        teile.append(f"⚠️ Davon überfällig: {len(überfällig)}")
    teile.append(f"🔴 Wichtige Aufgaben: {len(wichtige)}")
    if todos:
        teile.append("\nAlle offenen To-dos:")
        teile.append(todos_als_liste_text(todos))
    teile.append("\n💪 Neue Woche – frischer Start!")
    text = "\n".join(teile)
    for cid in chat_ids:
        await ctx.bot.send_message(chat_id=cid, text=text)


# ── Start ────────────────────────────────────────────────────

def main():
    lade_chat_ids()
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("liste",    cmd_liste))
    app.add_handler(CommandHandler("heute",    cmd_heute))
    app.add_handler(CommandHandler("erledigt", cmd_erledigt))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("ideen",    cmd_ideen))
    app.add_handler(CommandHandler("fokus",    cmd_fokus))
    app.add_handler(CommandHandler("suche",    cmd_suche))
    app.add_handler(CallbackQueryHandler(callback_rueckgaengig, pattern="^rueckgaengig$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, sprachnachricht))
    app.add_handler(MessageHandler(filters.PHOTO, foto_nachricht))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, textnachricht))

    jq = app.job_queue
    jq.run_daily(morgen_erinnerung,     dtime(hour=8,  minute=0, tzinfo=ZEITZONE))
    jq.run_daily(abend_zusammenfassung, dtime(hour=20, minute=0, tzinfo=ZEITZONE))
    jq.run_daily(wochen_review,         dtime(hour=19, minute=0, tzinfo=ZEITZONE))

    app.run_polling()

if __name__ == "__main__":
    main()
