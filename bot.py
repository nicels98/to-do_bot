import os, json
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from groq import Groq
from anthropic import Anthropic
from notion_client import Client as NotionClient

load_dotenv()

groq    = Groq(api_key=os.environ["GROQ_API_KEY"])
claude  = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
notion  = NotionClient(auth=os.environ["NOTION_TOKEN"])
DB      = os.environ["NOTION_DATABASE_ID"]


def transkribiere(path: str) -> str:
    with open(path, "rb") as f:
        return groq.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="de"
        ).text

def extrahiere_todos(text: str) -> list[str]:
    r = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content":
            f"Extrahiere alle Aufgaben/To-dos aus diesem Text. "
            f"Antworte NUR mit einer JSON-Liste von Strings, z.B. [\"Aufgabe 1\", \"Aufgabe 2\"]. "
            f"Kein anderer Text, keine Erklärung, nur die Liste. Text:\n{text}"}]
    )
    antwort = r.content[0].text.strip()
    if "```" in antwort:
        antwort = antwort.split("```")[1]
        if antwort.startswith("json"):
            antwort = antwort[4:]
    return json.loads(antwort.strip())

def todo_hinzufügen(titel: str):
    notion.pages.create(
        parent={"database_id": DB},
        properties={
            "Name":     {"title":    [{"text": {"content": titel}}]},
            "Erledigt": {"checkbox": False},
        }
    )

def offene_todos() -> list[dict]:
    seiten = notion.databases.query(
        database_id=DB,
        filter={"property": "Erledigt", "checkbox": {"equals": False}}
    )["results"]
    return [
        {"id": s["id"], "titel": s["properties"]["Name"]["title"][0]["plain_text"]}
        for s in seiten if s["properties"]["Name"]["title"]
    ]

def als_erledigt_markieren(page_id: str):
    notion.pages.update(page_id=page_id, properties={"Erledigt": {"checkbox": True}})


async def sprachnachricht(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Transkribiere...")
    voice = update.message.voice or update.message.audio
    file  = await ctx.bot.get_file(voice.file_id)
    path  = f"/tmp/{voice.file_id}.ogg"
    await file.download_to_drive(path)
    try:
        text  = transkribiere(path)
        todos = extrahiere_todos(text)
        if not todos:
            await msg.edit_text(f"Transkription:\n{text}\n\nKeine To-dos gefunden.")
            return
        for todo in todos:
            todo_hinzufügen(todo)
        liste = "\n".join(f"• {t}" for t in todos)
        await msg.edit_text(f"✅ {len(todos)} To-do(s) in Notion:\n\n{liste}")
    except Exception as e:
        await msg.edit_text(f"Fehler: {e}")
    finally:
        os.remove(path)

async def cmd_erledigt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    tasten = [[InlineKeyboardButton(t["titel"], callback_data=t["id"])] for t in todos]
    await update.message.reply_text(
        "Was hast du erledigt?",
        reply_markup=InlineKeyboardMarkup(tasten)
    )

async def erledigt_geklickt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    titel = next(
        btn.text for row in q.message.reply_markup.inline_keyboard
        for btn in row if btn.callback_data == q.data
    )
    await q.answer()
    als_erledigt_markieren(q.data)
    await q.edit_message_text(f"✅ Erledigt: {titel}")

async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    todos = offene_todos()
    if not todos:
        await update.message.reply_text("Keine offenen To-dos! 🎉")
        return
    text = "\n".join(f"• {t['titel']}" for t in todos)
    await update.message.reply_text(f"Offene To-dos:\n\n{text}")


app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, sprachnachricht))
app.add_handler(CommandHandler("erledigt", cmd_erledigt))
app.add_handler(CommandHandler("liste", cmd_liste))
app.add_handler(CallbackQueryHandler(erledigt_geklickt))
print("Bot läuft...")
app.run_polling()
