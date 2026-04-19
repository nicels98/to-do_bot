import os, json
import pytz
from datetime import time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from groq import Groq
from anthropic import Anthropic
from notion_client import Client as NotionClient

load_dotenv()

groq   = Groq(api_key=os.environ["GROQ_API_KEY"])
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
notion = NotionClient(auth=os.environ["NOTION_TOKEN"])
DB     = os.environ["NOTION_DATABASE_ID"]

ZEITZONE = pytz.timezone("Europe/Berlin")
CHAT_IDS_FILE = "/tmp/chat_ids.txt"
chat_ids = set()


# ── Chat-ID speichern ───────────────────────────────────────

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


# ── Kernfunktionen ──────────────────────────────────────────

def transkribiere(path: str) -> str:
    with open(path, "rb") as f:
        return groq.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="de"
        ).text

def analysiere(text: str, offene: list[dict]) -> dict:
    offene_str = "\n".join(f"- {t['titel']}" for t in offene) if offene else "(keine)"
    r = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": f"""Analysiere diese Sprachnachricht und antworte NUR mit JSON.

Sprachnachricht: "{text}"

Offene To-dos:
{offene_str}

Wähle eines dieser Formate:

Neue Aufgabe(n):
{{"aktion": "neu_todo", "todos": [{{"titel": "Aufgabe", "prioritaet": "wichtig"}}]}}
(prioritaet ist entweder "wichtig" oder "neutral")

Neue Notiz (keine Aufgabe, nur eine Information oder Gedanke):
{{"aktion": "neu_notiz", "titel": "kurzer Titel", "inhalt": "vollständiger Text"}}

Notiz zu einem bestehenden To-do hinzufügen:
{{"aktion": "notiz_zu_todo", "titel": "Titel aus der offenen Liste der am besten passt", "notiz": "der zusätzliche Text"}}

Aufgabe erledigt:
{{"aktion": "erledigt", "titel": "Titel aus der offenen Liste der am besten passt"}}

Nur JSON, kein anderer Text."""}]
    )
    antwort = r.content[0].text.strip()
    if "```" in antwort:
        antwort = antwort.split("```")[1]
        if antwort.startswith("json"):
            antwort = antwort[4:]
    return json.loads(antwort.strip())

def todo_hinzufügen(titel: str, prioritaet: str = "neutral"):
    notion.pages.create(
        parent={"database_id": DB},
        properties={
            "Name":      {"title":    [{"text": {"content": titel}}]},
            "Erledigt":  {"checkbox": False},
            "Priorität": {"select":   {"name": "Wichtig" if prioritaet == "wichtig" else "Neutral"}},
            "Typ":       {"select":   {"name": "To-do"}},
        }
    )

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
    seite = notion.pages.retrieve(page_id=page_id)
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
            {"property": "Typ", "select": {"equals": "To-do"}},
        ]}
    )["results"]
    todos = []
    for s in seiten:
        if not s["properties"]["Name"]["title"]:
            continue
        prioritaet_prop = s["properties"].get("Priorität") or {}
        select          = prioritaet_prop.get("select") or {}
        todos.append({
            "id":         s["id"],
            "titel":      s["properties"]["Name"]["title"][0]["plain_text"],
            "prioritaet": select.get("name", "Neutral"),
        })
    # Wichtig zuerst
    todos.sort(key=lambda t: 0 if t["prioritaet"] == "Wichtig" else 1)
    return todos

def als_erledigt_markieren(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Erledigt": {"checkbox": True}})

def todos_als_liste_text(todos: list[dict]) -> str:
    zeilen = []
    for i, t in enumerate(todos, 1):
        emoji = "🔴" if t["prioritaet"] == "Wichtig" else "⚪"
        zeilen.append(f"{i}. {emoji} {t['titel']}")
    return "\n".join(zeilen)


# ── Telegram Handler ────────────────────────────────────────

async def sprachnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    msg = await update.message.reply_text("Verarbeite...")
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
                todo_hinzufügen(todo["titel"], todo.get("prioritaet", "neutral"))
            liste = "\n".join(
                f"{'🔴' if todo.get('prioritaet') == 'wichtig' else '⚪'} {todo['titel']}"
                for todo in todos
            )
            await msg.edit_text(f"✅ {len(todos)} To-do(s) hinzugefügt:\n\n{liste}")

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
                await msg.edit_text("Kein passendes To-do gefunden. Nutze /liste um deine To-dos zu sehen.")

        elif result["aktion"] == "erledigt":
            gesuchter_titel = result.get("titel", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                als_erledigt_markieren(treffer["id"])
                await msg.edit_text(f"✅ Erledigt: {treffer['titel']}")
            else:
                await msg.edit_text(f"Transkription:\n{text}\n\nKein passendes To-do gefunden. Nutze /erledigt.")

    except Exception as e:
        await msg.edit_text(f"Fehler: {e}")
    finally:
        os.remove(path)


async def textnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet Text-Eingaben: 'erledigt', 'erledigt 2', '1', '2', ..."""
    speichere_chat_id(update.effective_chat.id)
    text = update.message.text.strip().lower()

    todos = offene_todos()

    # ── "erledigt N" direkt in einem Schritt ──
    if text.startswith("erledigt"):
        teile = text.split()
        if len(teile) == 2 and teile[1].isdigit():
            n = int(teile[1])
            if 1 <= n <= len(todos):
                todo = todos[n - 1]
                als_erledigt_markieren(todo["id"])
                await update.message.reply_text(f"✅ Erledigt: {todo['titel']}")
            else:
                await update.message.reply_text(f"Nummer {n} existiert nicht. Du hast {len(todos)} offene To-dos.")
            return

        # ── nur "erledigt" → Liste anzeigen ──
        if not todos:
            await update.message.reply_text("Keine offenen To-dos! 🎉")
            return
        liste = todos_als_liste_text(todos)
        ctx.user_data["todo_liste"] = todos
        await update.message.reply_text(
            f"Welche Aufgabe ist erledigt? Antworte mit der Nummer:\n\n{liste}"
        )
        return

    # ── Nur eine Zahl eingegeben → aus gespeicherter Liste abhaken ──
    if text.isdigit():
        n = int(text)
        # Frische Liste holen (falls zwischenzeitlich was geändert)
        gespeichert = ctx.user_data.get("todo_liste", todos)
        if 1 <= n <= len(gespeichert):
            todo = gespeichert[n - 1]
            als_erledigt_markieren(todo["id"])
            ctx.user_data.pop("todo_liste", None)
            await update.message.reply_text(f"✅ Erledigt: {todo['titel']}")
        else:
            await update.message.reply_text(
                f"Nummer {n} existiert nicht. Tippe 'erledigt' um die aktuelle Liste zu sehen."
            )
        return

    # ── Andere Textnachrichten ignorieren ──
    await update.message.reply_text(
        "Schick mir eine Sprachnachricht oder nutze:\n"
        "/liste – offene To-dos\n"
        "/erledigt – Aufgabe abhaken\n"
        "'erledigt 1' – Aufgabe 1 direkt abhaken"
    )


async def cmd_erledigt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    liste = todos_als_liste_text(todos)
    ctx.user_data["todo_liste"] = todos
    await update.message.reply_text(
        f"Welche Aufgabe ist erledigt? Antworte mit der Nummer:\n\n{liste}"
    )

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    liste = todos_als_liste_text(todos)
    await update.message.reply_text(f"📋 Offene To-dos:\n\n{liste}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "👋 Hey! Ich bin dein To-do Bot.\n\n"
        "Schick mir eine Sprachnachricht mit deinen Aufgaben oder Notizen.\n\n"
        "Befehle:\n"
        "/liste – offene To-dos\n"
        "/erledigt – Aufgabe abhaken\n"
        "Oder tippe 'erledigt 1' um Aufgabe 1 direkt abzuhaken."
    )

async def morgen_erinnerung(ctx: ContextTypes.DEFAULT_TYPE):
    todos = offene_todos()
    if not todos:
        text = "☀️ Guten Morgen! Heute keine offenen To-dos – freier Tag! 🎉"
    else:
        wichtige = [t for t in todos if t["prioritaet"] == "Wichtig"]
        normale  = [t for t in todos if t["prioritaet"] != "Wichtig"]
        zeilen = []
        if wichtige:
            zeilen.append("🔴 Wichtig:")
            zeilen += [f"  • {t['titel']}" for t in wichtige]
        if normale:
            zeilen.append("⚪ Neutral:")
            zeilen += [f"  • {t['titel']}" for t in normale]
        text = f"☀️ Guten Morgen! Du hast {len(todos)} offene To-dos:\n\n" + "\n".join(zeilen)
    for cid in chat_ids:
        await ctx.bot.send_message(chat_id=cid, text=text)

async def abend_zusammenfassung(ctx: ContextTypes.DEFAULT_TYPE):
    todos = offene_todos()
    if not todos:
        text = "🌙 Guten Abend! Alle To-dos erledigt – super gemacht! 🎉"
    else:
        liste = todos_als_liste_text(todos)
        text = f"🌙 Guten Abend! Noch {len(todos)} offene To-dos:\n\n{liste}"
    for cid in chat_ids:
        await ctx.bot.send_message(chat_id=cid, text=text)


# ── Start ───────────────────────────────────────────────────

def main():
    lade_chat_ids()
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("liste",    cmd_liste))
    app.add_handler(CommandHandler("erledigt", cmd_erledigt))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, sprachnachricht))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, textnachricht))

    jq = app.job_queue
    jq.run_daily(morgen_erinnerung,  time(hour=8,  minute=0, tzinfo=ZEITZONE))
    jq.run_daily(abend_zusammenfassung, time(hour=20, minute=0, tzinfo=ZEITZONE))

    app.run_polling()

if __name__ == "__main__":
    main()
