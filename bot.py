import os, json
import pytz
from datetime import time as dtime, datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
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


# ── Chat-ID speichern ────────────────────────────────────────

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

Wähle eines dieser Formate:

Neue Aufgabe(n):
{{"aktion": "neu_todo", "todos": [{{"titel": "Aufgabe", "prioritaet": "wichtig", "faelligkeit": "2024-01-15", "kategorie": "Marketing"}}]}}
- prioritaet: "wichtig" oder "neutral"
- faelligkeit: ISO-Datum NUR wenn ein Zeitraum/Datum genannt wird (morgen, übermorgen, in X Tagen, nächste Woche, DD.MM.YYYY etc.), sonst null
- kategorie: eine aus [{kategorien_str}] wenn erkennbar, sonst null

Neue Notiz:
{{"aktion": "neu_notiz", "titel": "kurzer Titel", "inhalt": "vollständiger Text"}}

Notiz zu bestehendem To-do:
{{"aktion": "notiz_zu_todo", "titel": "Titel aus der Liste", "notiz": "Text"}}

Aufgabe erledigt:
{{"aktion": "erledigt", "titel": "Titel aus der Liste"}}

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
                    faelligkeit: str = None, kategorie: str = None):
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
    notion.pages.create(parent={"database_id": DB}, properties=props)

def notiz_hinzufügen(titel: str, inhalt: str):
    notion.pages.create(
        parent={"database_id": DB},
        properties={
            "Name":    {"title":     [{"text": {"content": titel}}]},
            "Typ":     {"select":    {"name": "Notiz"}},
            "Notizen": {"rich_text": [{"text": {"content": inhalt}}]},
        }
    )

def notiz_zu_todo_hinzufügen(page_id: str, neue_notiz: str):
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
        prio_prop = s["properties"].get("Priorität") or {}
        prio_sel  = prio_prop.get("select") or {}
        fäll_prop = s["properties"].get("Fälligkeit") or {}
        fäll_date = fäll_prop.get("date") or {}
        kat_prop  = s["properties"].get("Kategorie") or {}
        kat_sel   = kat_prop.get("select") or {}
        todos.append({
            "id":          s["id"],
            "titel":       s["properties"]["Name"]["title"][0]["plain_text"],
            "prioritaet":  prio_sel.get("name", "Neutral"),
            "faelligkeit": fäll_date.get("start"),
            "kategorie":   kat_sel.get("name"),
        })
    heute = datetime.now(ZEITZONE).date()
    def sort_key(t):
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

def als_erledigt_markieren(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Erledigt": {"checkbox": True}})

def todos_als_liste_text(todos: list[dict], nummeriert: bool = True) -> str:
    zeilen = []
    for i, t in enumerate(todos, 1):
        emoji  = "🔴" if t["prioritaet"] == "Wichtig" else "⚪"
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
        f"- {t['titel']} | Priorität: {t['prioritaet']} | Fällig: {t.get('faelligkeit') or 'offen'} | Kategorie: {t.get('kategorie') or '-'}"
        for t in todos
    )
    r = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""Du bist ein produktiver eCom Business-Assistent. Heute ist {heute}.
Wähle die Top 3 To-dos für heute und erkläre kurz warum. Antworte auf Deutsch, direkt und motivierend.

To-dos:
{todos_str}

Format:
🎯 Deine Top 3 für heute:
1. [Titel] – [kurze Begründung]
2. [Titel] – [kurze Begründung]
3. [Titel] – [kurze Begründung]"""}]
    )
    return r.content[0].text.strip()


# ── Telegram Handler ─────────────────────────────────────────

async def sprachnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    msg   = await update.message.reply_text("Verarbeite...")
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
            if not todos:
                await msg.edit_text(f"Transkription:\n{text}\n\nKeine To-dos erkannt.")
                return
            for todo in todos:
                todo_hinzufügen(
                    todo["titel"],
                    todo.get("prioritaet", "neutral"),
                    todo.get("faelligkeit"),
                    todo.get("kategorie"),
                )
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
            await msg.edit_text(f"✅ {len(todos)} To-do(s) hinzugefügt:\n\n" + "\n".join(zeilen))

        elif result["aktion"] == "neu_notiz":
            notiz_hinzufügen(result["titel"], result["inhalt"])
            await msg.edit_text(f"📝 Notiz gespeichert:\n\n{result['inhalt']}")

        elif result["aktion"] == "notiz_zu_todo":
            gesuchter_titel = result.get("titel", "")
            notiz_text      = result.get("notiz", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                notiz_zu_todo_hinzufügen(treffer["id"], notiz_text)
                await msg.edit_text(f"📝 Notiz zu '{treffer['titel']}' ergänzt:\n\n{notiz_text}")
            else:
                await msg.edit_text("Kein passendes To-do gefunden. Nutze /liste.")

        elif result["aktion"] == "erledigt":
            gesuchter_titel = result.get("titel", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                als_erledigt_markieren(treffer["id"])
                await msg.edit_text(f"✅ Erledigt: {treffer['titel']}")
            else:
                await msg.edit_text(
                    f"Transkription:\n{text}\n\nKein passendes To-do gefunden. Nutze /erledigt."
                )

    except Exception as e:
        await msg.edit_text(f"Fehler: {e}")
    finally:
        os.remove(path)

async def textnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    text  = update.message.text.strip().lower()
    todos = offene_todos()

    if text.startswith("erledigt"):
        teile = text.split()
        if len(teile) == 2 and teile[1].isdigit():
            n = int(teile[1])
            if 1 <= n <= len(todos):
                todo = todos[n - 1]
                als_erledigt_markieren(todo["id"])
                await update.message.reply_text(f"✅ Erledigt: {todo['titel']}")
            else:
                await update.message.reply_text(
                    f"Nummer {n} existiert nicht. Du hast {len(todos)} offene To-dos."
                )
            return
        if not todos:
            await update.message.reply_text("Keine offenen To-dos! 🎉")
            return
        ctx.user_data["todo_liste"] = todos
        await update.message.reply_text(
            f"Welche Aufgabe ist erledigt? Antworte mit der Nummer:\n\n{todos_als_liste_text(todos)}"
        )
        return

    if text.isdigit():
        n           = int(text)
        gespeichert = ctx.user_data.get("todo_liste", todos)
        if 1 <= n <= len(gespeichert):
            todo = gespeichert[n - 1]
            als_erledigt_markieren(todo["id"])
            ctx.user_data.pop("todo_liste", None)
            await update.message.reply_text(f"✅ Erledigt: {todo['titel']}")
        else:
            await update.message.reply_text(
                "Nummer nicht gefunden. Tippe 'erledigt' für die aktuelle Liste."
            )
        return

    await update.message.reply_text(
        "Schick mir eine Sprachnachricht oder nutze:\n"
        "/liste – alle To-dos\n"
        "/heute – dringende To-dos\n"
        "/briefing – KI-Tagesplan\n"
        "/erledigt – Aufgabe abhaken\n"
        "'erledigt 1' – direkt abhaken"
    )

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "👋 Hey! Ich bin dein To-do Bot.\n\n"
        "Schick mir eine Sprachnachricht mit deinen Aufgaben.\n\n"
        "Befehle:\n"
        "/liste – alle To-dos\n"
        "/heute – dringende & heutige To-dos\n"
        "/briefing – KI-Tagesplan (Top 3)\n"
        "/erledigt – Aufgabe abhaken\n"
        "'erledigt 1' – direkt abhaken"
    )

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    await update.message.reply_text(f"📋 Offene To-dos:\n\n{todos_als_liste_text(todos)}")

async def cmd_heute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos     = offene_todos()
    heute_str = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    relevant  = [
        t for t in todos
        if (t.get("faelligkeit") and t["faelligkeit"] <= heute_str)
        or (not t.get("faelligkeit") and t["prioritaet"] == "Wichtig")
    ]
    if not relevant:
        await update.message.reply_text(
            "Heute nichts Dringendes! 🎉\nNutze /liste für alle To-dos."
        )
        return
    await update.message.reply_text(f"📅 Heute relevant:\n\n{todos_als_liste_text(relevant)}")

async def cmd_erledigt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    ctx.user_data["todo_liste"] = todos
    await update.message.reply_text(
        f"Welche Aufgabe ist erledigt? Antworte mit der Nummer:\n\n{todos_als_liste_text(todos)}"
    )

async def cmd_briefing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    msg   = await update.message.reply_text("Analysiere deine To-dos...")
    todos = offene_todos()
    if not todos:
        await msg.edit_text("Keine offenen To-dos – du bist up to date! 🎉")
        return
    top3 = await ki_top3(todos)
    await msg.edit_text(top3)

async def morgen_erinnerung(ctx: ContextTypes.DEFAULT_TYPE):
    todos        = offene_todos()
    heute_str    = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    überfällig   = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] < heute_str]
    heute_fällig = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] == heute_str]
    teile = [f"☀️ Guten Morgen! {len(todos)} offene To-dos."]
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
    # Nur sonntags ausführen (6 = Sonntag in Python)
    if datetime.now(ZEITZONE).weekday() != 6:
        return
    todos      = offene_todos()
    heute_str  = datetime.now(ZEITZONE).strftime("%Y-%m-%d")
    überfällig = [t for t in todos if t.get("faelligkeit") and t["faelligkeit"] < heute_str]
    wichtige   = [t for t in todos if t["prioritaet"] == "Wichtig"]
    teile = [
        "📊 Wöchentlicher Review\n",
        f"📋 Offene To-dos gesamt: {len(todos)}",
    ]
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
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, sprachnachricht))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, textnachricht))

    jq = app.job_queue
    jq.run_daily(morgen_erinnerung,     dtime(hour=8,  minute=0, tzinfo=ZEITZONE))
    jq.run_daily(abend_zusammenfassung, dtime(hour=20, minute=0, tzinfo=ZEITZONE))
    jq.run_daily(wochen_review,         dtime(hour=19, minute=0, tzinfo=ZEITZONE))

    app.run_polling()

if __name__ == "__main__":
    main()
