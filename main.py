import os
import sys
import time
import logging
import asyncio
import threading
import datetime
import random
import aiosqlite
import telethon
from flask import Flask
from telethon import TelegramClient, events, Button, functions, types
from telethon.errors import FloodWaitError, UserNotParticipantError, UserBlockedError, InputUserDeactivatedError

# ================= 1. CONFIGURATION =================
logging.basicConfig(
    format='%(asctime)s - [%(levelname)s] - %(name)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("UltraBot_18")

# Check Version
print(f"\n\n🍆 CURRENT TELETHON VERSION: {telethon.__version__}\n\n")

def get_env(key, default, cast_type=str):
    val = os.environ.get(key, str(default)).strip()
    try:
        if cast_type == bool:
            return val.lower() in ('true', '1', 'yes', 'on')
        return cast_type(val)
    except: return default

# --- Credentials ---
API_ID = get_env("API_ID", 32962599, int)
API_HASH = get_env("API_HASH", "7b4d1f82086a615b3cf0b447fb5d30db")
BOT_TOKEN = get_env("BOT_TOKEN", "8584774121:AAG3qalg6twMv5D2T_YUxXUU4B7C9Vo9ij8")

ADMIN_ID = get_env("ADMIN_ID", 6792645837, int)
SOURCE_CHANNEL_ID = get_env("SOURCE_CHANNEL_ID", -1003425606503, int)
FORCE_CHANNEL_ID = get_env("FORCE_CHANNEL_ID", -1003306799796, int)
FORCE_JOIN_LINK = get_env("FORCE_JOIN_LINK", "https://t.me/+OOmFTd7Zlzo2Njk1")

STRICT_FORCE_JOIN = True
DB_FILE = "bot_data.db"

# ⚠️ ডিফল্ট ভিডিও রেঞ্জ (DB থেকে লোড হবে)
VIDEO_START_ID = 1
VIDEO_END_ID = 500

# এডমিন ইনপুট হ্যান্ডেল করার জন্য স্টেট
ADMIN_STATE = {} # {admin_id: "WAITING_FOR_TYPE"}

# ================= 2. DATABASE MANAGER =================
class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.init_schema()
        logger.info("✅ Database & Schema Loaded 💋")

    async def init_schema(self):
        # Users Table
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned BOOLEAN DEFAULT 0,
                is_verified BOOLEAN DEFAULT 0
            )
        ''')
        # Settings Table for Video Range
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await self.conn.commit()

    # --- User Methods ---
    async def add_user(self, user_id, first_name, username, referrer_id=None):
        try:
            async with self.conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as c:
                if await c.fetchone():
                    await self.conn.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
                    await self.conn.commit()
                    return False
            if referrer_id == user_id: referrer_id = None
            await self.conn.execute(
                "INSERT INTO users (user_id, first_name, username, referrer_id, is_verified) VALUES (?, ?, ?, ?, 0)",
                (user_id, first_name, username, referrer_id)
            )
            await self.conn.commit()
            return True
        except: return False

    async def verify_age(self, user_id):
        await self.conn.execute("UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def is_age_verified(self, user_id):
        async with self.conn.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,)) as c:
            r = await c.fetchone()
            return r['is_verified'] if r and r['is_verified'] else False

    # --- Ban System ---
    async def get_ban_status(self, user_id):
        async with self.conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as c:
            r = await c.fetchone()
            return r['is_banned'] if r else False

    async def set_ban_status(self, user_id, is_banned):
        await self.conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if is_banned else 0, user_id))
        await self.conn.commit()

    async def get_user_id_by_username(self, username):
        username = username.replace("@", "")
        async with self.conn.execute("SELECT user_id FROM users WHERE username = ? COLLATE NOCASE", (username,)) as c:
            r = await c.fetchone()
            return r['user_id'] if r else None

    # --- Stats & Info ---
    async def get_user_name(self, user_id):
        async with self.conn.execute("SELECT first_name FROM users WHERE user_id = ?", (user_id,)) as c:
            r = await c.fetchone()
            return r['first_name'] if r else "Unknown"

    async def get_all_users_id(self):
        async with self.conn.execute("SELECT user_id FROM users") as c:
            return [r['user_id'] for r in await c.fetchall()]

    async def get_leaderboard(self, limit=10):
        async with self.conn.execute('SELECT referrer_id, COUNT(user_id) as refs FROM users WHERE referrer_id IS NOT NULL GROUP BY referrer_id ORDER BY refs DESC LIMIT ?', (limit,)) as c:
            return await c.fetchall()
            
    async def get_recent_joins(self, limit=5):
        async with self.conn.execute('SELECT first_name, user_id FROM users ORDER BY joined_at DESC LIMIT ?', (limit,)) as c:
            return await c.fetchall()

    async def get_dashboard_stats(self):
        now = datetime.datetime.now()
        day_ago = now - datetime.timedelta(hours=24)
        async with self.conn.execute("SELECT COUNT(*) FROM users") as c: total = (await c.fetchone())[0]
        async with self.conn.execute("SELECT COUNT(*) FROM users WHERE joined_at > ?", (day_ago,)) as c: active = (await c.fetchone())[0]
        async with self.conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as c: banned = (await c.fetchone())[0]
        return {"total": total, "active": active, "banned": banned}

    # --- Settings (Video Range) ---
    async def set_video_range(self, end_id):
        await self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('video_end', ?)", (str(end_id),))
        await self.conn.commit()

    async def get_video_range(self):
        async with self.conn.execute("SELECT value FROM settings WHERE key='video_end'") as c:
            r = await c.fetchone()
            return int(r['value']) if r else 500

db = DatabaseManager(DB_FILE)
client = TelegramClient('ultra_bot_session', API_ID, API_HASH)
app = Flask(__name__)
user_last_msg = {} 

# ================= 3. HELPERS =================
async def is_user_banned(user_id):
    if user_id == ADMIN_ID: return False
    return await db.get_ban_status(user_id)

async def check_force_join(user_id):
    if user_id == ADMIN_ID: return True
    try:
        p = await client(functions.channels.GetParticipantRequest(channel=FORCE_CHANNEL_ID, participant=user_id))
        return not isinstance(p.participant, (types.ChannelParticipantLeft, types.ChannelParticipantBanned))
    except: return not STRICT_FORCE_JOIN

async def safe_delete(chat_id, user_id):
    msg_id = user_last_msg.get(user_id)
    if msg_id:
        try: await client.delete_messages(chat_id, msg_id)
        except: pass
    if user_id in user_last_msg: del user_last_msg[user_id]

def mask_text(text):
    return (text[:3] + "***") if text and len(text) > 3 else "Unknown"

# 🔥 AUTO-FIX CHANNEL ID
def fix_id(cid):
    try:
        cid_str = str(cid)
        if not cid_str.startswith("-100"):
            return int(f"-100{cid_str}")
        return int(cid)
    except: return cid

SOURCE_CHANNEL_ID = fix_id(SOURCE_CHANNEL_ID)
FORCE_CHANNEL_ID = fix_id(FORCE_CHANNEL_ID)

# ================= 4. HANDLERS =================

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.chat_id in [SOURCE_CHANNEL_ID, FORCE_CHANNEL_ID]: return

    # Group Restriction
    if not event.is_private:
        return

    user = await event.get_sender()
    if not user: return
    
    if await is_user_banned(user.id):
        return await event.reply("🚫 **Get lost! You are banned.**")

    args = event.text.split()
    referrer = int(args[1].replace('ref', '')) if len(args) > 1 and args[1].startswith('ref') else None
    is_new = await db.add_user(user.id, user.first_name, user.username, referrer)
    if is_new and referrer:
        try: await client.send_message(referrer, f"🔥 **New Partner Joined!**\n{user.first_name} entered the room 😈")
        except: pass

    # Age Verification
    if not await db.is_age_verified(user.id):
        msg = (
            "🔞 **WARNING: 18+ CONTENT** 🔞\n\n"
            "Hey there... 👋\n"
            "This bot contains **highly s*nsitive & h*t** materials.\n\n"
            "⚠️ **Are you 18 years or older?**\n"
            "Kids stay away! Only for legends. 🤫"
        )
        buttons = [
            [Button.inline("😈 YES, I'm 18+ (Enter)", data="verify_age")],
            [Button.inline("👶 NO, I'm a Kid (Exit)", data="deny_age")]
        ]
        sent = await event.reply(msg, buttons=buttons)
        user_last_msg[user.id] = sent.id
        return

    # Force Join Check
    if not await check_force_join(user.id):
        msg = "🔒 **Locked Content!** 🥵\n\nTo unlock the *hidden treasure*, you must join our channel first."
        buttons = [[Button.url("👙 Join Channel", FORCE_JOIN_LINK)], [Button.inline("✅ Unlocked? Click Here", data="check_sub")]]
        sent = await event.reply(msg, buttons=buttons)
        user_last_msg[user.id] = sent.id
        return

    await show_dashboard(event, user)

async def show_dashboard(event, user):
    await safe_delete(event.chat_id, user.id)
    ref_link = f"https://t.me/{(await client.get_me()).username}?start=ref{user.id}"
    
    msg = (
        f"💋 **Hey Naughty {user.first_name}...**\n\n"
        f"Welcome to the world of pleasure 😈\n"
        f"My collection is wet & ready for you 💦\n\n"
        f"🔗 **Invite Friends:**\n`{ref_link}`\n\n"
        f"👇 *Touch the button below to start...* 🥵"
    )
    
    buttons = [
        [Button.inline("🔥 Watch H*t V*deos 🔞", data="get_vid")],
        [Button.inline("🏆 Playboys/Playgirls", data="leaderboard"), Button.url("📢 Join Channel", FORCE_JOIN_LINK)]
    ]
    if user.id == ADMIN_ID:
        buttons.append([Button.inline("🛡 Admin Panel", data="admin_panel")])

    sent = await event.respond(msg, buttons=buttons)
    user_last_msg[user.id] = sent.id

# ================= 5. ADMIN PANEL LOGIC =================

async def get_admin_panel_text():
    global VIDEO_END_ID
    stats = await db.get_dashboard_stats()
    top_refs = await db.get_leaderboard(3)
    recent = await db.get_recent_joins(3)
    
    # Referrer Text
    ref_txt = "_No data available_"
    if top_refs:
        ref_txt = ""
        for i, row in enumerate(top_refs, 1):
             name = await db.get_user_name(row['referrer_id'])
             ref_txt += f" {i}. {name[:10]}.. - {row['refs']} refs\n"

    # Recent Joins Text
    join_txt = "_No data available_"
    if recent:
        join_txt = ""
        for row in recent:
            join_txt += f" 👤 {row['first_name'][:12]}..\n"

    text = (
        "🛡 **ADMIN CONTROL PANEL**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 **Statistics**\n"
        f"👥 Total Users: `{stats['total']}`\n"
        f"🔥 Active (24h): `{stats['active']}`\n"
        f"🚫 Banned: `{stats['banned']}`\n"
        f"📹 Video Range: `1 - {VIDEO_END_ID}`\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🏆 **Top Referrers**\n"
        f"{ref_txt}\n"
        "🆕 **Recent Joins**\n"
        f"{join_txt}"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ **Status:** 🟢 Online"
    )
    return text

async def get_admin_buttons():
    return [
        [Button.inline("📢 Broadcast", data="adm_bc")],
        [Button.inline("🚫 Ban User", data="adm_ban"), Button.inline("✅ Unban User", data="adm_unban")],
        [Button.inline("🔄 Refresh Stats ⚡", data="admin_panel")],
        [Button.inline("➕ Video Range", data="adm_range_plus"), Button.inline("➖ Video Range", data="adm_range_minus")],
        [Button.inline("🔙 Back", data="back_home")]
    ]

# ================= 6. CALLBACK HANDLERS =================
@client.on(events.CallbackQuery)
async def callback_handler(event):
    global VIDEO_END_ID
    if event.chat_id in [SOURCE_CHANNEL_ID, FORCE_CHANNEL_ID]: return
    user_id = event.sender_id
    data = event.data.decode('utf-8')

    if await is_user_banned(user_id): return await event.answer("🚫 Banned!", alert=True)

    # --- Video Range Controls ---
    if data == "adm_range_plus":
        if user_id != ADMIN_ID: return
        ADMIN_STATE[user_id] = "WAITING_PLUS_RANGE"
        await event.reply("➕ **Increase Range:**\n\nHow many videos do you want to ADD?\nType the number (e.g. `10` to add 10 videos).")
        await event.answer()
        return

    elif data == "adm_range_minus":
        if user_id != ADMIN_ID: return
        ADMIN_STATE[user_id] = "WAITING_MINUS_RANGE"
        await event.reply("➖ **Decrease Range:**\n\nHow many videos do you want to REMOVE?\nType the number (e.g. `10` to remove 10 videos).")
        await event.answer()
        return

    # --- Ban/Unban Controls ---
    elif data == "adm_ban":
        if user_id != ADMIN_ID: return
        ADMIN_STATE[user_id] = "WAITING_BAN_INPUT"
        await event.reply("🚫 **Ban User:**\n\nSend `User ID` or `@Username` to ban.")
        await event.answer()
        return

    elif data == "adm_unban":
        if user_id != ADMIN_ID: return
        ADMIN_STATE[user_id] = "WAITING_UNBAN_INPUT"
        await event.reply("✅ **Unban User:**\n\nSend `User ID` or `@Username` to unban.")
        await event.answer()
        return

    elif data == "adm_bc":
        if user_id != ADMIN_ID: return
        ADMIN_STATE[user_id] = "WAITING_BROADCAST"
        await event.reply("📢 **Broadcast:**\n\nSend the message (Text, Image, Video) you want to broadcast.")
        await event.answer()
        return

    # --- Admin Panel Main ---
    elif data == "admin_panel":
        if user_id == ADMIN_ID:
            try:
                txt = await get_admin_panel_text()
                btns = await get_admin_buttons()
                await event.edit(txt, buttons=btns)
            except Exception as e:
                await event.answer("Panel Error!", alert=True)
                print(e)
        return

    # --- Normal User Logic ---
    if data not in ["check_sub", "deny_age", "verify_age"]:
        if not await check_force_join(user_id):
            await event.answer("⚠️ You left the channel! Join back first.", alert=True)
            msg = "🔒 **Access Denied!** 🚫\n\nYou left our VIP channel. Join back to continue."
            buttons = [[Button.url("👙 Join Channel", FORCE_JOIN_LINK)], [Button.inline("✅ Check & Enter", data="check_sub")]]
            await event.edit(msg, buttons=buttons)
            return

    if data == "verify_age":
        await db.verify_age(user_id)
        await event.answer("✅ Welcome 😈", alert=True)
        if not await check_force_join(user_id):
            msg = "🔒 **One last step baby...** 💋\n\nJoin our channel to see the magic."
            buttons = [[Button.url("👙 Join Channel", FORCE_JOIN_LINK)], [Button.inline("✅ Check & Enter", data="check_sub")]]
            await event.edit(msg, buttons=buttons)
        else:
            await event.delete()
            await show_dashboard(event, await client.get_entity(user_id))
        return

    elif data == "deny_age":
        await event.answer("👶 Go drink milk kiddo!", alert=True)
        await event.edit("🚫 **Access Denied.**\nCome back when you grow up.")
        return

    elif data == "get_vid":
        if not await db.is_age_verified(user_id):
            await event.answer("⚠️ Verification Required!", alert=True)
            return

        await event.answer("💦 Finding something spicy... Wait baby 🥵")
        found = False
        
        for _ in range(15): 
            vid_id = random.randint(VIDEO_START_ID, VIDEO_END_ID)
            try:
                msg = await client.get_messages(SOURCE_CHANNEL_ID, ids=vid_id)
                
                if msg and msg.media:
                    await safe_delete(event.chat_id, user_id)
                    
                    sent = await client.send_message(
                        event.chat_id,
                        message=f"🔥 **H*t Clip Found!** 🔞\n\n🤫 *Enjoy privately...*\n🔗 {FORCE_JOIN_LINK}",
                        file=msg.media,
                        buttons=[
                            [Button.inline("🔁 Next V*deo 💦", data="get_vid")], 
                            [Button.inline("🔙 Main Menu", data="back_home")]
                        ],
                        link_preview=False
                    )
                    if event.is_private: user_last_msg[user_id] = sent.id
                    await event.delete()
                    found = True
                    break 
            except FloodWaitError as e:
                await event.answer(f"⏳ Wait {e.seconds}s 💋", alert=True)
                return
            except Exception as e:
                continue 
        
        if not found:
            await event.answer("❌ Server is busy darling... Try again 💋", alert=True)

    elif data == "check_sub":
        if await check_force_join(user_id):
            await event.delete()
            await show_dashboard(event, await client.get_entity(user_id))
        else:
            await event.answer("❌ You didn't join yet! Don't cheat me 🥺", alert=True)

    elif data == "leaderboard":
        lb = await db.get_leaderboard(10)
        txt = "🏆 **Top 10 Playboys/Playgirls** 😈\n\n" + ("_No data yet_" if not lb else "")
        for i, row in enumerate(lb, 1):
             name = await db.get_user_name(row['referrer_id'])
             txt += f"{i}. {mask_text(name)} : **{row['refs']}** partners\n"
        await event.edit(txt, buttons=[[Button.inline("🔙 Back", data="back_home")]])

    elif data == "back_home":
        await event.delete()
        await show_dashboard(event, await client.get_entity(user_id))

# ================= 7. ADMIN INPUT LISTENER =================
@client.on(events.NewMessage)
async def admin_input_handler(event):
    global VIDEO_END_ID
    user_id = event.sender_id
    if user_id != ADMIN_ID or user_id not in ADMIN_STATE: return

    state = ADMIN_STATE[user_id]
    text = event.text.strip()
    
    # --- Broadcast ---
    if state == "WAITING_BROADCAST":
        users = await db.get_all_users_id()
        sent = await event.reply(f"🚀 Broadcasting to {len(users)} users...")
        count = 0
        for uid in users:
            try:
                await client.send_message(uid, event.message)
                count += 1
                await asyncio.sleep(0.05)
            except: pass
        await sent.edit(f"✅ **Broadcast Complete!**\nSent to: {count} users.")
        del ADMIN_STATE[user_id]
        return

    # --- Ban/Unban ---
    elif state in ["WAITING_BAN_INPUT", "WAITING_UNBAN_INPUT"]:
        target_id = None
        
        if text.isdigit():
            target_id = int(text)
        elif text.startswith("@"):
            target_id = await db.get_user_id_by_username(text)
        else:
            # Try as username without @
            target_id = await db.get_user_id_by_username(text)

        if not target_id:
            await event.reply("❌ **User not found!** Ensure they have used the bot.")
        else:
            is_ban = (state == "WAITING_BAN_INPUT")
            await db.set_ban_status(target_id, is_ban)
            status = "Banned" if is_ban else "Unbanned"
            await event.reply(f"✅ User `{target_id}` has been **{status}**.")
        
        del ADMIN_STATE[user_id]
        return

    # --- Video Range +/- ---
    elif state in ["WAITING_PLUS_RANGE", "WAITING_MINUS_RANGE"]:
        if not text.isdigit():
            await event.reply("❌ Please enter a valid number.")
            return
        
        amount = int(text)
        old_range = VIDEO_END_ID
        
        if state == "WAITING_PLUS_RANGE":
            VIDEO_END_ID += amount
            await event.reply(f"📈 **Range Increased!**\nOld: {old_range}\nNew: {VIDEO_END_ID}")
        else:
            VIDEO_END_ID -= amount
            if VIDEO_END_ID < 1: VIDEO_END_ID = 1
            await event.reply(f"📉 **Range Decreased!**\nOld: {old_range}\nNew: {VIDEO_END_ID}")
            
        await db.set_video_range(VIDEO_END_ID)
        del ADMIN_STATE[user_id]
        return

# ================= 8. RUNNER =================
@app.route('/')
def index(): return "Bot Running 24/7 🔥"
def run_flask(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

async def main():
    global VIDEO_END_ID
    await db.connect()
    
    # Load Video Range
    VIDEO_END_ID = await db.get_video_range()
    print(f"📹 Loaded Video Range: 1 - {VIDEO_END_ID}")

    while True:
        try:
            print("🚀 Connecting...")
            await client.start(bot_token=BOT_TOKEN)
            print("✅ Connected!")
            break
        except FloodWaitError as e:
            print(f"⚠️ FloodWait: Sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"❌ Error: {e}")
            return

    threading.Thread(target=run_flask, daemon=True).start()
    await client.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass