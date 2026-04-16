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

def offene_todos() -> list[dict]:
    seiten = notion.databases.query(
        database_id=DB,
        filter={"and": [
            {"property": "Erledigt", "checkbox": {"equals": False}},
            {"property": "Typ", "select": {"equals": "To-do"}},
        ]}
    )["results"]
    return [
        {
            "id": s["id"],
            "titel": s["properties"]["Name"]["title"][0]["plain_text"],
            "prioritaet": s["properties"].get("Priorität", {}).get("select", {}).get("name", "Neutral"),
        }
        for s in seiten if s["properties"]["Name"]["title"]
    ]

def als_erledigt_markieren(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Erledigt": {"checkbox": True}})


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
                f"{'🔴' if t.get('prioritaet') == 'wichtig' else '⚪'} {t['titel']}"
                for t in todos
            )
            await msg.edit_text(f"✅ {len(todos)} To-do(s) hinzugefügt:\n\n{liste}")

        elif result["aktion"] == "neu_notiz":
            notiz_hinzufügen(result["titel"], result["inhalt"])
            await msg.edit_text(f"📝 Notiz gespeichert:\n\n{result['inhalt']}")

        elif result["aktion"] == "erledigt":
            gesuchter_titel = result.get("titel", "")
            treffer = next((t for t in offene if t["titel"] == gesuchter_titel), None)
            if not treffer:
                treffer = next((t for t in offene if gesuchter_titel.lower() in t["titel"].lower()), None)
            if treffer:
                als_erledigt_markieren(treffer["id"])
                await msg.edit_text(f"✅ Erledigt: {treffer['titel']}")
            else:
                await msg.edit_text("Kein passendes To-do gefunden. Nutze /erledigt zum manuellen Abhaken.")

    except Exception as e:
        await msg.edit_text(f"Fehler: {e}")
    finally:
        os.remove(path)

async def cmd_erledigt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    tasten = [[InlineKeyboardButton(
        f"{'🔴 ' if t['prioritaet'] == 'Wichtig' else '⚪ '}{t['titel']}",
        callback_data=t["id"]
    )] for t in todos]
    await update.message.reply_text(
        "Was hast du erledigt?",
        reply_markup=InlineKeyboardMarkup(tasten)
    )

async def erledigt_geklickt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    titel = next(
        btn.text for row in q.message.reply_markup.inline_keyboard
        for btn in row if btn.callback_data == q.data
    )
    als_erledigt_markieren(q.data)
    await q.edit_message_text(f"✅ Erledigt: {titel}")

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    speichere_chat_id(update.effective_chat.id)
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    wichtige = [t for t in todos if t["prioritaet"] == "Wichtig"]
    normale  = [t for t in todos if t["prioritaet"] != "Wichtig"]
    text = ""
    if wichtige:
        text += "🔴 Wichtig:\n" + "\n".join(f"• {t['titel']}" for t in wichtige) + "\n\n"
    if normale:
        text += "⚪ Neutral:\n" + "\n".join(f"• {t['titel']}" for t in normale)
    await update.message.reply_text(f"Offene To-dos:\n\n{text}")


# ── Geplante Nachrichten ────────────────────────────────────

async def morgen_erinnerung(ctx):
    todos = offene_todos()
    if not todos:
        nachricht = "☀️ Guten Morgen! Keine offenen To-dos. Freier Tag! 🎉"
    else:
        wichtige = [t for t in todos if t["prioritaet"] == "Wichtig"]
        normale  = [t for t in todos if t["prioritaet"] != "Wichtig"]
        nachricht = f"☀️ Guten Morgen! Du hast {len(todos)} offene To-do(s):\n\n"
        if wichtige:
            nachricht += "🔴 Wichtig:\n" + "\n".join(f"• {t['titel']}" for t in wichtige) + "\n\n"
        if normale:
            nachricht += "⚪ Neutral:\n" + "\n".join(f"• {t['titel']}" for t in normale)
    for chat_id in chat_ids:
        await ctx.bot.send_message(chat_id=chat_id, text=nachricht)

async def abend_zusammenfassung(ctx):
    todos = offene_todos()
    nachricht = "🌙 Tages-Zusammenfassung\n\n"
    if not todos:
        nachricht += "Alle To-dos erledigt — top! 🎉"
    else:
        nachricht += f"Noch {len(todos)} offen:\n\n"
        for t in todos:
            emoji = "🔴" if t["prioritaet"] == "Wichtig" else "⚪"
            nachricht += f"{emoji} {t['titel']}\n"
    for chat_id in chat_ids:
        await ctx.bot.send_message(chat_id=chat_id, text=nachricht)


# ── Start ───────────────────────────────────────────────────

lade_chat_ids()
app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()

app.job_queue.run_daily(morgen_erinnerung,    time=time(8,  0, tzinfo=ZEITZONE))
app.job_queue.run_daily(abend_zusammenfassung, time=time(20, 0, tzinfo=ZEITZONE))

app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, sprachnachricht))
app.add_handler(CommandHandler("erledigt", cmd_erledigt))
app.add_handler(CommandHandler("liste", cmd_liste))
app.add_handler(CallbackQueryHandler(erledigt_geklickt))
print("Bot läuft...")
app.run_polling()
